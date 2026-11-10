"""tw / tierwright command line.

  tw init [--backend subscription|api|both] [--per-day USD] [--per-goal USD] [--goal TEXT --done CMD]
  tw adopt [--no-model] [--refresh] [--map-limit N]   codebase + goal -> validated step ladder, GAP.md
  tw plan                                         same as adopt (for a new, empty project)
  tw step show|list|check [ID]                    the ladder; check runs the open step's gate
  tw done                                         run the goal's done bar
  tw check 'CHECK'                                run one check (file:, words:, contains:, lacks:, review:, run:)
  tw submit JOB [--name N] [-- ARGS]              queue a job (needs an output contract); ARGS go to queue.submit_cmd
  tw queue run [--watch] | stop NAME              run jobs (ssh queue: pull verdicts); the dispatcher acts on each end
  tw dispatch                                     process unread events now
  tw brief JOB                                    the compact JSON a rule or model reads
  tw contract lint FILE... | --help               check contract syntax
  tw rule list | add --name .. --re .. --action .. --class .. --incident .. [--job J] | backtest ...
  tw ask ROLE [FILE|-]                            call a role through the router (JSON in, JSON out)
  tw route [ROLE]                                 which tier / backend / model each role resolves to
  tw spend                                        model spend by role, tier, backend
  tw ledger [dnr] [-n N]                          recent ledger lines, or the do-not-repeat list
  tw mine [--days N] | mine apply ID [--force]    build the infrastructure agents repeat most
  tw tool [NAME]                                  list / run mined tools
  tw digest PATH                                  cached summary of a file agents re-read
  tw status                                       <= 20 lines; also writes STATUS.md, HANDOFF.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__


def _p():
    from .project import Project, find_root
    return Project(find_root()).ensure()


def _cfg(p):
    from . import config
    return config.load(p)


def cmd_init(a):
    from . import init
    r = init.run(".", a.backend, a.per_day, a.per_goal, a.goal, a.done or None,
                 probe=not a.no_probe, force=a.force,
                 interactive=None if not a.yes else False, lite=a.lite)
    print(f"tierwright initialized ({r['mode']}), backend {r['backend']}: " + ", ".join(r["wrote"]))
    for b, v in r["probes"].items():
        print(f"probe {b}: {v}")
    if any(v != "ok" for v in r["probes"].values()):
        print("a backend probe failed: fix it or re-run `tw init --force --backend ...`")
        return 1
    print("next: edit goal.yaml, then `tw adopt`")
    return 0


def cmd_adopt(a):
    from . import adopt
    p = _p()
    r = adopt.plan(p, _cfg(p), use_model=not a.no_model, map_limit=a.map_limit, refresh=a.refresh)
    present = sum(1 for v in r["fit"].values() if v["status"] != "missing")
    print(f"{len(r['steps'])} new step(s) from {r['source']}; {present}/{len(r['fit'])} components covered; "
          f"{r['summarized_now']} module(s) summarized. Read GAP.md; next: `tw step show`.")
    return 0


def cmd_step(a):
    from . import steps
    p = _p()
    if a.action == "list":
        for s in steps.load(p):
            print(f"{s.get('status', '?'):7} {s['id']:24} {s.get('tier', '?')}  {s.get('goal', '')}")
        return 0
    if a.action == "show":
        s = steps.current(p) if not a.id else next((x for x in steps.load(p) if x["id"] == a.id), None)
        if not s:
            print("no open step: `tw adopt` to plan, or `tw done` if all passed")
            return 1
        print(Path(s["_file"]).read_text())
        return 0
    r = steps.check(p, a.id)
    for x in r["results"]:
        print(f"{'ok  ' if x['rc'] == 0 else 'FAIL'} {x['cmd']} ({x['s']}s)")
        if x["rc"] != 0:
            print("     " + "\n     ".join(x["tail"]))
    print(f"step {r['step']}: {'PASSED' if r['pass'] else 'FAILED'}" + (f"; next open: {r['next']}" if r["next"] else ""))
    return 0 if r["pass"] else 1


def cmd_done(a):
    from . import steps
    r = steps.done(_p())
    for x in r["results"]:
        print(f"{'ok  ' if x['rc'] == 0 else 'FAIL'} {x['cmd']}")
    print("DONE BAR MET" if r["pass"] else "done bar not met")
    return 0 if r["pass"] else 1


def cmd_check(a):
    from . import checks
    p = _p()
    r = checks.run(p, a.check, _cfg(p))
    print(f"{'ok  ' if r['rc'] == 0 else 'FAIL'} {r['cmd']}")
    for t in r["tail"]:
        print("     " + t)
    return 0 if r["rc"] == 0 else 1


def cmd_submit(a):
    from . import config, jobqueue
    p = _p()
    cfg = _cfg(p)
    need = cfg["mode"] != "lite"
    if cfg["queue"]["backend"] == "ssh":
        from . import remote
        name, warn = remote.Remote(p, cfg).submit(a.file, a.name, a.extra, require_contract=need)
        for w in warn[:8]:
            print(f"warning (the remote verifier decides): {w}", file=sys.stderr)
        if len(warn) > 8:
            print(f"warning: {len(warn) - 8} more", file=sys.stderr)
        print(f"queued {name} on {cfg['queue']['host']}")
        return 0
    if a.extra:
        print("tw: extra submit arguments need queue.backend: ssh with queue.submit_cmd", file=sys.stderr)
        return 2
    print("queued", jobqueue.submit(p, a.file, a.name, require_contract=need,
                                    ext=config.contract_ext(cfg)))
    return 0


def _on_end(p, cfg):
    from . import dispatch

    def f(meta):
        for d in dispatch.poll(p, cfg):
            print(f"[dispatch] {d['job']}: {d['action']} ({d.get('rule') or d['class']}) -> {d.get('applied')}")
        if meta:
            v = meta["contract"]
            print(f"[end] {meta['job']} rc={meta['rc']} {meta['reason']} contract={'pass' if v['pass'] else 'FAIL'}")
    return f


def cmd_queue(a):
    from . import jobqueue
    p = _p()
    cfg = _cfg(p)
    ssh = cfg["queue"]["backend"] == "ssh"
    if a.action == "stop":
        if ssh:
            from . import remote
            ok = remote.Remote(p, cfg).stop(a.name)
        else:
            ok = jobqueue.stop(p, a.name)
        print("stopped" if ok else f"{a.name} is not pending")
        return 0 if ok else 1
    if ssh:
        from . import remote
        remote.run(p, cfg, once=not a.watch, on_end=_on_end(p, cfg))
        open_ = remote.Remote(p, cfg).tracked()
        if open_:
            print(f"on {cfg['queue']['host']}: " + ", ".join(f"{j} ({r['state']})" for j, r in open_.items()))
        else:
            print(f"on {cfg['queue']['host']}: no open jobs")
        return 0
    jobqueue.run(p, once=not a.watch, default_timeout=cfg["limits"]["job_timeout_s"], on_end=_on_end(p, cfg))
    return 0


def cmd_dispatch(a):
    p = _p()
    _on_end(p, _cfg(p))(None)
    return 0


def cmd_brief(a):
    from . import brief
    print(json.dumps(brief.build(_p(), a.job), indent=1))
    return 0


def cmd_contract(a):
    from . import contract
    if not a.files:
        print(contract.__doc__)
        return 0
    ext = None
    try:
        from . import config
        ext = config.contract_ext(_cfg(_p()))
    except Exception:
        pass                # outside a project: the built-in dialect
    bad = 0
    for f in a.files:
        try:
            warn: list = []
            cs = contract.parse(Path(f).read_text(), ext, warn)
            for w in warn:
                print(f"{f}: warning: {w}")
            real = [c for c in cs if c.kind not in ("log", "after", "mode")]
            print(f"{f}: {len(real)} check(s)" + ("" if real else "  NO CONTRACT"))
            bad += not real
        except (contract.ContractError, OSError) as e:
            print(f"{f}: {e}")
            bad += 1
    return 1 if bad else 0


def cmd_rule(a):
    from . import rules
    p = _p()
    if a.action == "list":
        for r in rules.load(p):
            print(f"{r.name:22} {r.action:9} {r.cls:12} {r.incident}")
        return 0
    d = {"name": a.name, "re": a.re, "action": a.rule_action, "class": a.cls, "incident": a.incident}
    if a.rc is not None:
        d["rc"] = a.rc
    if a.action == "backtest":
        bt = rules.backtest(p, rules.from_dict(d), a.job)
    else:
        bt = rules.add(p, d, a.job)
    for c in bt["changed"]:
        print(f"changes {c['job']}: {c['from']} -> {c['to']}")
    for e in bt["errors"]:
        print("REFUSED:", e)
    if a.action == "add" and bt["ok"]:
        print(f"rule {a.name} added to .tierwright/rules.yaml")
    return 0 if bt["ok"] else 1


def cmd_ask(a):
    from . import router
    p = _p()
    raw = sys.stdin.read() if a.file in (None, "-") else Path(a.file).read_text()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = raw
    print(json.dumps(router.call(p, _cfg(p), a.role, payload), indent=1))
    return 0


def cmd_route(a):
    from . import config
    cfg = _cfg(_p())
    for r in ([a.role] if a.role else list(config.DEFAULT_ROLES)):
        x = config.role(cfg, r)
        print(f"{r:10} {x['tier']}  {x['backend']:12} {x.get('model', '-')} {x.get('effort', '')}")
    return 0


def cmd_spend(a):
    from . import router
    sm = router.summary(_p())
    if not sm:
        print("no model calls yet")
    for k, v in sorted(sm.items(), key=lambda kv: -kv[1]["cost_usd"]):
        print(f"{k:34} {v['calls']:4} calls  ${v['cost_usd']:.4f}  in {v['in']}  out {v['out']}  rejected {v['rejected']}")
    return 0


def cmd_ledger(a):
    from . import ledger
    p = _p()
    out = ledger.do_not_repeat(p) if a.what == "dnr" else ledger.lines(p, a.n)
    print("\n".join(out) or "(empty)")
    return 0


def cmd_mine(a):
    from . import mine
    p = _p()
    if a.action == "apply":
        if not a.id:
            print("tw mine apply ID")
            return 2
        print(mine.apply(p, _cfg(p), a.id, force=a.force))
        return 0
    r = mine.run(p, days=a.days, min_count=a.min, dirs=a.transcripts, check=not a.no_check)
    print(f"{r['transcripts']} transcript(s), {r['calls']} tool calls, ~{r['tokens_seen']} tokens of tool output")
    for x in r["proposals"][:12]:
        print(f"{x['id']} {x['kind']:6} {x['count']:3}x ~{x['est_tokens']:>7} tok  "
              f"{'ok ' if x['valid'] else 'no '} {x.get('name') or x.get('path') or x['why'][:60]}")
    if not r["proposals"]:
        print("nothing repeats often enough yet")
    for n, u in r["applied"].items():
        print(f"installed {n}: used {u['uses']}x since {u['since']}")
    print("details: MINE.md; install: tw mine apply ID")
    return 0


def cmd_tool(a):
    from . import mine
    p = _p()
    if not a.name:
        idx = mine.tools_index(p)
        for n, t in idx.items():
            print(f"{n:24} {'; '.join(t['cmds'])[:90]}")
        if not idx:
            print("no tools yet: `tw mine`")
        return 0
    return mine.run_tool(p, a.name)


def cmd_digest(a):
    from . import mine
    p = _p()
    print(mine.digest(p, _cfg(p), a.path))
    return 0


def cmd_status(a):
    from . import status
    print(status.write(_p()), end="")
    return 0


def build() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tw", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"tierwright {__version__}")
    sp = ap.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("init", help="set up a project (asks subscription / api / both)")
    s.add_argument("--backend", choices=["subscription", "api", "both"])
    s.add_argument("--per-day", type=float)
    s.add_argument("--per-goal", type=float)
    s.add_argument("--goal")
    s.add_argument("--done", action="append", help="done-bar command (repeatable)")
    s.add_argument("--no-probe", action="store_true", help="skip the one-call backend check")
    s.add_argument("--force", action="store_true")
    s.add_argument("--yes", "-y", action="store_true", help="never prompt")
    s.add_argument("--lite", action="store_true",
                   help="no job contracts; plain-language checks (file:, words:, contains:, review:)")
    s.set_defaults(f=cmd_init)

    for name in ("adopt", "plan"):
        s = sp.add_parser(name, help="codebase + goal -> validated step ladder and GAP.md")
        s.add_argument("--no-model", action="store_true", help="T0 only: plan from the fit table")
        s.add_argument("--refresh", action="store_true", help="re-plan the steps not yet passed")
        s.add_argument("--map-limit", type=int, default=60)
        s.set_defaults(f=cmd_adopt)

    s = sp.add_parser("step", help="the step ladder")
    s.add_argument("action", choices=["show", "list", "check"])
    s.add_argument("id", nargs="?")
    s.set_defaults(f=cmd_step)

    sp.add_parser("done", help="run the goal's done bar").set_defaults(f=cmd_done)

    s = sp.add_parser("check", help="run one check now, e.g. tw check 'words: out/post.md 300-500'")
    s.add_argument("check")
    s.set_defaults(f=cmd_check)

    s = sp.add_parser("submit", help="queue a job with an output contract")
    s.add_argument("file")
    s.add_argument("--name")
    s.set_defaults(f=cmd_submit)

    s = sp.add_parser("queue", help="run or stop jobs")
    s.add_argument("action", choices=["run", "stop"])
    s.add_argument("name", nargs="?")
    s.add_argument("--watch", action="store_true", help="keep running and wait for new jobs")
    s.set_defaults(f=cmd_queue)

    sp.add_parser("dispatch", help="process unread events").set_defaults(f=cmd_dispatch)

    s = sp.add_parser("brief", help="the compact JSON for one finished job")
    s.add_argument("job")
    s.set_defaults(f=cmd_brief)

    s = sp.add_parser("contract", help="lint contracts; no files = the contract reference")
    s.add_argument("lint", nargs="?", choices=["lint"])
    s.add_argument("files", nargs="*")
    s.set_defaults(f=cmd_contract)

    s = sp.add_parser("rule", help="triage rules")
    s.add_argument("action", choices=["list", "add", "backtest"])
    s.add_argument("--name")
    s.add_argument("--re")
    s.add_argument("--rc", type=int)
    s.add_argument("--action", dest="rule_action", choices=["ok", "ignore", "resubmit", "diagnose"])
    s.add_argument("--class", dest="cls")
    s.add_argument("--incident")
    s.add_argument("--job", help="the incident job the rule must match")
    s.set_defaults(f=cmd_rule)

    s = sp.add_parser("ask", help="call a role through the router")
    s.add_argument("role")
    s.add_argument("file", nargs="?")
    s.set_defaults(f=cmd_ask)

    s = sp.add_parser("route", help="role -> tier / backend / model")
    s.add_argument("role", nargs="?")
    s.set_defaults(f=cmd_route)

    sp.add_parser("spend", help="model spend by role / tier / backend").set_defaults(f=cmd_spend)

    s = sp.add_parser("ledger", help="recent ledger lines, or `dnr` for do-not-repeat")
    s.add_argument("what", nargs="?", choices=["dnr"])
    s.add_argument("-n", type=int, default=15)
    s.set_defaults(f=cmd_ledger)

    s = sp.add_parser("mine", help="turn what agents repeat most into tools, rules and digests")
    s.add_argument("action", nargs="?", choices=["apply"])
    s.add_argument("id", nargs="?")
    s.add_argument("--days", type=float, default=14)
    s.add_argument("--min", type=int, default=3, help="minimum repeats")
    s.add_argument("--transcripts", nargs="*", help="transcript dirs/files (default: this project's)")
    s.add_argument("--no-check", action="store_true", help="rank only, skip validation")
    s.add_argument("--force", action="store_true", help="install a tool that is not read-only (unverified)")
    s.set_defaults(f=cmd_mine)

    s = sp.add_parser("tool", help="list or run mined tools")
    s.add_argument("name", nargs="?")
    s.set_defaults(f=cmd_tool)

    s = sp.add_parser("digest", help="cached T1 summary of a file (refreshed when it changes)")
    s.add_argument("path")
    s.set_defaults(f=cmd_digest)

    sp.add_parser("status", help="<= 20 lines; writes STATUS.md and HANDOFF.md").set_defaults(f=cmd_status)
    return ap


def main(argv: list[str] | None = None) -> int:
    from .project import ProjectError
    from .contract import ContractError
    from .router import BudgetExceeded, CallFailed
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if argv[:1] == ["submit"] and "--" in argv:   # tw submit JOB [--name N] -- ARGS for submit_cmd
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    a = build().parse_args(argv)
    a.extra = extra
    if a.cmd == "rule" and a.action in ("add", "backtest"):
        miss = [k for k in ("name", "rule_action", "cls", "incident") if not getattr(a, k)]
        if miss:
            print("tw rule " + a.action + " needs --" + ", --".join(m.replace("rule_", "").replace("cls", "class") for m in miss))
            return 2
    if a.cmd == "queue" and a.action == "stop" and not a.name:
        print("tw queue stop NAME")
        return 2
    try:
        return a.f(a)
    except (ProjectError, ContractError, BudgetExceeded, CallFailed, FileNotFoundError, ValueError) as e:
        print(f"tw: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
