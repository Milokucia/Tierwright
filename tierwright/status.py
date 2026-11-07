"""`tw status`: the whole project in <= 20 lines, also written to STATUS.md and
HANDOFF.md. People and agents read this, never raw logs."""

from __future__ import annotations

from . import config, jobqueue, ledger, router, steps
from .project import Project, clip, stamp


def lines(p: Project) -> list[str]:
    cfg = config.load(p)
    out = [f"Tierwright status {stamp()} UTC, {cfg['mode']} mode, backend {cfg['backend']}"]
    try:
        g = steps.load_goal(p)
        out.append(f"goal: {clip(g['goal'], 100)}")
    except steps.StepError as e:
        out.append(f"goal: {e}")
    st = steps.load(p)
    if st:
        n_pass = sum(1 for s in st if s.get("status") == "passed")
        cur = steps.current(p)
        out.append(f"steps: {n_pass}/{len(st)} passed"
                   + (f"; current {cur['id']} ({cur['status']}, {cur.get('tier')}): {clip(cur.get('goal', ''), 70)}"
                      if cur else ""))
    else:
        out.append("steps: none yet (`tw adopt`)")
    c = jobqueue.counts(p)
    out.append(f"queue: {c['pending']} pending, {c['running']} running, {c['done']} done")
    att = sorted(p.attention.glob("*.json")) if p.attention.exists() else []
    if att:
        out.append(f"attention: {len(att)} ({', '.join(a.stem for a in att[-4:])})")
    day = router._day_start()
    sm = router.summary(p, since=day)
    if sm:
        tot = sum(v["cost_usd"] for v in sm.values())
        calls = sum(v["calls"] for v in sm.values())
        out.append(f"spend today: ${tot:.2f} over {calls} model calls")
        for k, v in sorted(sm.items(), key=lambda kv: -kv[1]["cost_usd"])[:3]:
            out.append(f"  {k}: {v['calls']} calls, ${v['cost_usd']:.2f}, {v['rejected']} rejected")
    else:
        out.append("spend today: $0.00 (no model calls)")
    if p.owner_queue.exists():
        n = p.owner_queue.read_text().count("\n* **")
        if n:
            out.append(f"owner queue: {n} item(s) in owner-queue.md")
    out += ["last:"] + ["  " + x for x in ledger.lines(p, 5)]
    return out[:20]


def write(p: Project) -> str:
    ls = lines(p)
    txt = "\n".join(ls) + "\n"
    (p.root / "STATUS.md").write_text("```\n" + txt + "```\n")
    cur = steps.current(p)
    handoff = ["# HANDOFF", "", f"Written by `tw status` {stamp()} UTC. Read this, not history.", "",
               "```", *ls, "```", ""]
    if cur:
        handoff += [f"## Next: {cur['id']}", "", cur.get("goal", ""), "",
                    *[f"- `{c['action']}` `{c['path']}`: {c['what']}" for c in cur.get("changes", [])],
                    "- gate: " + "; ".join(f"`{g}`" for g in cur.get("gate", [])), ""]
    dnr = ledger.do_not_repeat(p, 10)
    if dnr:
        handoff += ["## Do not repeat", "", *[f"- {x}" for x in dnr], ""]
    (p.root / "HANDOFF.md").write_text("\n".join(handoff))
    return txt
