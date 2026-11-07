"""`tw mine`: build the infrastructure your agents use most, so they stop paying for it.

Reads what agents actually did and turns the most expensive repeats into code:

  sources (T0)     Claude Code transcripts for this project
                   (~/.claude/projects/<project>/**/*.jsonl), plus Tierwright's
                   own briefs, dispatch verdicts and run records
  patterns (T0)    the same Bash command run again and again; the same
                   sequence of commands; the same file read again and again;
                   polling; the same unmatched failure diagnosed twice; a role
                   whose answers keep getting rejected
  proposals        tool    a script under .tierwright/tools/ that runs the
                           sequence in one call with trimmed output (`tw tool NAME`)
                   rule    a triage rule from a failure diagnosed twice
                   digest  a cached T1 summary of a file read again and again
                           (`tw digest PATH`), regenerated when the file changes
                   note    what needs a person (polling, a role's brief)
  validation       tool: every command must be read-only; the original commands
                   and the script both run, the script must exit 0 and carry
                   each original's first output line. rule: backtested
                   (rules.backtest). digest: the extract role's schema. A
                   proposal that fails validation is shown, never applied.
  ranking          estimated tokens: tool output re-read as context, plus a
                   fixed cost per model turn the script saves

    tw mine [--days 14] [--min 3]      write MINE.md, print the ranking
    tw mine apply ID [--force]         install one proposal (ledger line)
    tw tool [NAME]                     list / run installed tools
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from . import ledger, router, rules
from .project import Project, clip, now, stamp, write_json

TURN_TOKENS = 400          # rough cost of one extra model turn (tool call + reply), excl. output
CHARS_PER_TOKEN = 4
HEAD = 40                  # lines a tool prints per command
READ_ONLY = {
    "git": {"status", "log", "diff", "show", "branch", "rev-parse", "ls-files", "remote", "describe", "blame"},
    "tw": {"status", "brief", "step", "route", "spend", "ledger", "contract", "rule"},
    "tierwright": {"status", "brief", "step", "route", "spend", "ledger"},
}
READ_ONLY_EXE = {"ls", "cat", "head", "wc", "grep", "rg", "find", "pwd", "echo", "stat", "du", "df",
                 "which", "file", "tree", "sort", "uniq", "cut", "nproc", "uname", "date", "env"}
POLL_RE = re.compile(r"(\bsleep\s+\d|\bwatch\s|tail\s+-\w*f)")


# ── sources ──────────────────────────────────────────────────────────────────
def transcript_dir(root: Path) -> Path:
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root))


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text(c.get("text", c.get("content", ""))) if isinstance(c, dict) else str(c)
                         for c in content)
    return str(content or "")


def strip_cd(cmd: str, root: Path) -> str:
    cmd = " ".join(cmd.split())
    for r in {str(root), str(root).rstrip("/")}:
        for pre in (f"cd {r} && ", f"cd {shlex.quote(r)} && "):
            if cmd.startswith(pre):
                return cmd[len(pre):]
    return cmd


def key(tool: str, inp: dict, root: Path) -> str:
    if tool == "Bash":
        return "Bash: " + strip_cd(inp.get("command", ""), root)
    if tool == "Read":
        p = inp.get("file_path", "")
        try:
            p = os.path.relpath(p, root) if os.path.isabs(p) else p
        except ValueError:
            pass
        return f"Read: {p}"
    if tool in ("Grep", "Glob"):
        return f"{tool}: {inp.get('pattern', '')} {inp.get('path', '')}".strip()
    return f"{tool}: " + json.dumps(inp, sort_keys=True)[:200]


def calls(files: list[Path], root: Path) -> list[dict]:
    """Every tool call with its result size, in order, per session."""
    out = []
    for f in files:
        pending: dict[str, dict] = {}
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = d.get("message") or {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if d.get("type") == "assistant" and b.get("type") == "tool_use":
                    c = {"session": f.stem, "tool": b.get("name", ""), "input": b.get("input") or {},
                         "result_chars": 0, "error": False}
                    c["key"] = key(c["tool"], c["input"], root)
                    pending[b.get("id")] = c
                    out.append(c)
                elif d.get("type") == "user" and b.get("type") == "tool_result":
                    c = pending.pop(b.get("tool_use_id"), None)
                    if c is not None:
                        c["result_chars"] = len(_text(b.get("content")))
                        c["error"] = bool(b.get("is_error"))
    return out


def transcript_files(root: Path, days: float, dirs: list[str] | None = None) -> list[Path]:
    cut = now() - days * 86400
    out = []
    for d in ([Path(x) for x in dirs] if dirs else [transcript_dir(root)]):
        if d.is_file():
            out.append(d)
            continue
        out += [f for f in d.glob("**/*.jsonl") if f.stat().st_mtime >= cut]
    return sorted(out)


# ── patterns ─────────────────────────────────────────────────────────────────
def _tok(chars: int) -> int:
    return chars // CHARS_PER_TOKEN


def _pid(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:6]


def _name(cmds: list[str]) -> str:
    words = []
    for c in cmds:
        try:
            t = shlex.split(c)
        except ValueError:
            t = c.split()
        t = [x for x in t if re.match(r"^[A-Za-z][\w-]*$", x)][:2]
        words += t
    n = re.sub(r"[^a-z0-9]+", "-", "-".join(words[:4]).lower()).strip("-")
    return n[:32] or "tool"


def read_only(cmd: str) -> bool:
    if re.search(r"(>|\brm\b|\bmv\b|\bcp\b|\bsed\s+-i|\bchmod\b|\bkill\b|\bcurl\b|\bwget\b|\bpush\b|\bsudo\b)", cmd):
        return False
    for part in re.split(r"\s*(?:&&|\|\||;|\|)\s*", cmd):
        try:
            t = shlex.split(part)
        except ValueError:
            return False
        t = [x for x in t if not re.match(r"^\w+=", x)]
        if not t:
            continue
        exe = os.path.basename(t[0])
        if exe in READ_ONLY:
            sub = next((x for x in t[1:] if not x.startswith("-")), "")
            if sub not in READ_ONLY[exe]:
                return False
        elif exe == "tail":
            if any(re.match(r"-\w*f|--follow", x) for x in t[1:]):
                return False
        elif exe not in READ_ONLY_EXE:
            return False
    return True


def find(cs: list[dict], min_count: int = 3) -> dict:
    """Repeated commands, repeated sequences, repeated reads, polling."""
    bash = [c for c in cs if c["tool"] == "Bash" and not c["error"]]
    by_key = defaultdict(list)
    for c in bash:
        by_key[c["key"]].append(c)
    single = []
    for k, lst in by_key.items():
        if len(lst) >= min_count and not POLL_RE.search(k):
            single.append({"cmds": [k[6:]], "count": len(lst),
                           "tokens": sum(_tok(c["result_chars"]) for c in lst)})
    seqs = Counter()
    seq_tok = defaultdict(int)
    by_session = defaultdict(list)
    for c in cs:
        by_session[c["session"]].append(c)
    for lst in by_session.values():
        ks = [c for c in lst if c["tool"] == "Bash" and not c["error"]]
        for n in (3, 2):
            for i in range(len(ks) - n + 1):
                win = ks[i:i + n]
                kk = tuple(c["key"][6:] for c in win)
                if len(set(kk)) < n or any(POLL_RE.search(k) for k in kk):
                    continue
                seqs[kk] += 1
                seq_tok[kk] += sum(_tok(c["result_chars"]) for c in win)
    seq = []
    covered: set[tuple] = set()
    for kk, n in seqs.most_common():
        if n < min_count:
            break
        if any(set(kk) <= set(o) for o in covered):   # a 2-gram inside a frequent 3-gram
            continue
        covered.add(kk)
        seq.append({"cmds": list(kk), "count": n, "tokens": seq_tok[kk]})
    reads = Counter()
    read_tok = defaultdict(int)
    for c in cs:
        if c["tool"] == "Read":
            reads[c["key"][6:]] += 1
            read_tok[c["key"][6:]] += _tok(c["result_chars"])
    rd = [{"path": p, "count": n, "tokens": read_tok[p]} for p, n in reads.most_common()
          if n >= min_count and read_tok[p] >= 500]
    polls = Counter(c["key"][6:] for c in bash if POLL_RE.search(c["key"]))
    return {"single": single, "sequences": seq, "reads": rd,
            "polling": [{"cmd": k, "count": n} for k, n in polls.most_common(5) if n >= 2]}


def failure_signatures(p: Project) -> list[dict]:
    """Unmatched failures (rule None) whose first error line repeats: rule candidates."""
    groups = defaultdict(list)
    for b in rules.recorded_briefs(p):
        v = rules.triage(b, rules.load(p))
        if v["rule"] is not None or v["action"] != "diagnose":
            continue
        line = (b.get("errors") or b.get("tail") or [""])[-1]
        sig = re.sub(r"\d+", lambda m: r"\d+", re.escape(" ".join(line.split()[:8])))
        if len(sig) >= 12:
            groups[sig].append(b["job"])
    return [{"re": s, "jobs": j} for s, j in groups.items() if len(j) >= 2]


def role_rejections(p: Project, min_calls: int = 3) -> list[dict]:
    out = []
    for k, v in router.summary(p).items():
        if v["calls"] >= min_calls and v["rejected"] / v["calls"] > 0.3:
            out.append({"role": k, "calls": v["calls"], "rejected": v["rejected"]})
    return out


# ── proposals ────────────────────────────────────────────────────────────────
def script(cmds: list[str]) -> str:
    body = ["#!/usr/bin/env bash", "# Generated by `tw mine`: one call instead of "
            f"{len(cmds)}, output trimmed to {HEAD} lines per command.", "cd \"$(dirname \"$0\")/../..\"", ""]
    for c in cmds:
        body.append(f"echo '$ {c.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'")
        body.append(f"( {c} ) 2>&1 | head -n {HEAD}")
    return "\n".join(body) + "\n"


def propose(p: Project, pats: dict, sigs: list[dict], rej: list[dict]) -> list[dict]:
    props = []
    for s in pats["sequences"] + pats["single"]:
        cmds = s["cmds"]
        saved_turns = s["count"] * max(0, len(cmds) - 1)
        trimmed = max(0, s["tokens"] - s["count"] * len(cmds) * HEAD * 20 // CHARS_PER_TOKEN)
        est = saved_turns * TURN_TOKENS + trimmed
        if est < 200:
            continue
        props.append({"id": _pid("tool", cmds), "kind": "tool", "name": _name(cmds), "cmds": cmds,
                      "count": s["count"], "est_tokens": est,
                      "why": f"run {s['count']}x" + (f" as a {len(cmds)}-command sequence" if len(cmds) > 1 else "")})
    for r in pats["reads"]:
        props.append({"id": _pid("digest", r["path"]), "kind": "digest", "path": r["path"],
                      "count": r["count"], "est_tokens": int(r["tokens"] * 0.8),
                      "why": f"read {r['count']}x, ~{r['tokens']} tokens"})
    for s in sigs:
        props.append({"id": _pid("rule", s["re"]), "kind": "rule", "re": s["re"], "jobs": s["jobs"],
                      "count": len(s["jobs"]), "est_tokens": 3000 * len(s["jobs"]),
                      "why": f"same unmatched failure in {len(s['jobs'])} jobs: {', '.join(s['jobs'][:3])}"})
    for pl in pats["polling"]:
        props.append({"id": _pid("note", pl["cmd"]), "kind": "note", "count": pl["count"],
                      "est_tokens": pl["count"] * TURN_TOKENS,
                      "why": f"polled {pl['count']}x: `{clip(pl['cmd'], 80)}`; make it a job with a contract"})
    for r in rej:
        props.append({"id": _pid("note", r["role"]), "kind": "note", "count": r["calls"],
                      "est_tokens": r["rejected"] * 2000,
                      "why": f"{r['role']}: {r['rejected']}/{r['calls']} answers rejected; its brief or schema "
                             "is missing what it needs"})
    seen, out = set(), []
    for x in sorted(props, key=lambda x: -x["est_tokens"]):
        if x["id"] not in seen:
            seen.add(x["id"])
            out.append(x)
    return out


def _run(cmd: str, cwd: Path, timeout: float = 60) -> tuple[int, str]:
    try:
        pr = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return pr.returncode, pr.stdout + pr.stderr
    except subprocess.TimeoutExpired:
        return 124, ""


def validate(p: Project, x: dict) -> list[str]:
    if x["kind"] == "tool":
        bad = [c for c in x["cmds"] if not read_only(c)]
        if bad:
            return [f"not read-only, cannot verify safely: `{clip(bad[0], 80)}`"]
        tmp = p.state / "tools" / f".check-{x['id']}.sh"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(script(x["cmds"]))
        rc, out = _run(f"bash {shlex.quote(str(tmp))}", p.root)
        tmp.unlink(missing_ok=True)
        if rc != 0:
            return [f"script exited {rc}"]
        errs = []
        for c in x["cmds"]:
            orc, oout = _run(c, p.root)
            first = next((l for l in oout.splitlines() if l.strip()), "")
            if first and first not in out:
                errs.append(f"script output lacks `{clip(first, 60)}` from `{clip(c, 60)}`")
        return errs
    if x["kind"] == "rule":
        r = rules.from_dict(_rule_dict(x))
        return rules.backtest(p, r, x["jobs"][0])["errors"]
    if x["kind"] == "digest":
        return [] if (p.root / x["path"]).exists() else [f"{x['path']} no longer exists"]
    return ["a note: needs a person, nothing to install"]


def _rule_dict(x: dict) -> dict:
    return {"name": f"mined-{x['id']}", "re": x["re"], "action": "diagnose", "class": "mined",
            "incident": f"tw mine: {', '.join(x['jobs'][:3])}"}


# ── run / apply ──────────────────────────────────────────────────────────────
def tools_index(p: Project) -> dict:
    f = p.state / "tools.yaml"
    return (yaml.safe_load(f.read_text()) or {}) if f.exists() else {}


def tool_uses(cs: list[dict], name: str) -> int:
    return sum(1 for c in cs if c["tool"] == "Bash" and re.search(rf"\btw tool {re.escape(name)}\b", c["key"]))


def run(p: Project, days: float = 14, min_count: int = 3, dirs: list[str] | None = None,
        check: bool = True) -> dict:
    p.ensure()
    files = transcript_files(p.root, days, dirs)
    cs = calls(files, p.root)
    pats = find(cs, min_count)
    props = propose(p, pats, failure_signatures(p), role_rejections(p))
    for x in props:
        x["errors"] = validate(p, x) if check else ["not checked"]
        x["valid"] = not x["errors"]
    applied = {}
    for name, t in tools_index(p).items():
        applied[name] = {"since": t.get("since"), "uses": tool_uses(cs, name)}
    res = {"ts": now(), "transcripts": len(files), "calls": len(cs),
           "tokens_seen": sum(_tok(c["result_chars"]) for c in cs), "proposals": props,
           "applied": applied}
    write_json(p.state / "mine.json", res)
    (p.root / "MINE.md").write_text(render(res))
    return res


def render(r: dict) -> str:
    L = ["# MINE: what your agents repeat", "",
         f"Written by `tw mine` {stamp(r['ts'])} UTC from {r['transcripts']} transcript(s), "
         f"{r['calls']} tool calls, ~{r['tokens_seen']} tokens of tool output.", "",
         "| ID | Kind | Seen | Est. tokens saved | Valid | What |", "|---|---|---|---|---|---|"]
    for x in r["proposals"]:
        what = {"tool": lambda: f"`tw tool {x['name']}`: " + "; ".join(f"`{clip(c, 50)}`" for c in x["cmds"]),
                "digest": lambda: f"digest `{x['path']}`",
                "rule": lambda: f"rule `{clip(x['re'], 60)}`",
                "note": lambda: x["why"]}[x["kind"]]()
        ok = "yes" if x["valid"] else "no: " + clip("; ".join(x["errors"]), 80)
        L.append(f"| {x['id']} | {x['kind']} | {x['count']}x | ~{x['est_tokens']} | {ok} | {what} |")
    if not r["proposals"]:
        L.append("| - | - | - | - | - | nothing repeats often enough yet |")
    if r["applied"]:
        L += ["", "## Installed tools", "", "| Tool | Since | Uses since |", "|---|---|---|"]
        for n, a in r["applied"].items():
            L.append(f"| {n} | {a['since']} | {a['uses']} |")
    L += ["", "Install one: `tw mine apply ID`. Tools are read-only and verified against the commands "
          "they replace; rules are backtested; nothing is installed without passing.", ""]
    return "\n".join(L)


def apply(p: Project, cfg: dict, pid: str, force: bool = False) -> str:
    f = p.state / "mine.json"
    if not f.exists():
        raise ValueError("run `tw mine` first")
    res = json.loads(f.read_text())
    x = next((y for y in res["proposals"] if y["id"] == pid), None)
    if not x:
        raise ValueError(f"no proposal {pid!r} in the last `tw mine`")
    errs = validate(p, x)
    if errs and not (force and x["kind"] == "tool" and all(e.startswith("not read-only") for e in errs)):
        raise ValueError("not installed: " + "; ".join(errs))
    if x["kind"] == "tool":
        idx = tools_index(p)
        name = x["name"]
        while name in idx and idx[name]["cmds"] != x["cmds"]:
            name += "-2"
        sp = p.state / "tools" / f"{name}.sh"
        sp.write_text(script(x["cmds"]))
        sp.chmod(0o755)
        idx[name] = {"cmds": x["cmds"], "since": stamp(), "why": x["why"], "est_tokens": x["est_tokens"]}
        (p.state / "tools.yaml").write_text(yaml.safe_dump(idx, sort_keys=False))
        _claude_line(p, f"- `tw tool {name}`: runs " + "; ".join(f"`{clip(c, 40)}`" for c in x["cmds"])
                     + " in one call (mined: " + x["why"] + ").")
        msg = f"tool {name} installed: `tw tool {name}`"
    elif x["kind"] == "rule":
        bt = rules.add(p, _rule_dict(x), x["jobs"][0])
        if not bt["ok"]:
            raise ValueError("not installed: " + "; ".join(bt["errors"]))
        msg = f"rule mined-{x['id']} added (edit its action in .tierwright/rules.yaml once the fix is known)"
    elif x["kind"] == "digest":
        d = digest(p, cfg, x["path"])
        _claude_line(p, f"- Read `.tierwright/digests/{_dname(x['path'])}` before `{x['path']}` "
                        f"(mined: {x['why']}); `tw digest {x['path']}` refreshes it.")
        msg = f"digest written: {d}"
    else:
        raise ValueError("a note needs a person; nothing to install")
    ledger.record(p, "mine", x["id"], "unknown", note=msg, change=json.dumps(x.get("cmds") or x.get("path") or x.get("re")))
    return msg


def _claude_line(p: Project, line: str) -> None:
    cm = p.root / "CLAUDE.md"
    t = cm.read_text() if cm.exists() else "# CLAUDE.md\n"
    head = "### Mined tools (from `tw mine`)"
    if head not in t:
        t = t.rstrip() + f"\n\n{head}\n\n"
    if line not in t:
        t = t.rstrip() + "\n" + line + "\n"
    cm.write_text(t)


def _dname(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", path) + ".md"


def digest(p: Project, cfg: dict, path: str) -> str:
    """A cached T1 summary of a file, regenerated only when the file changes."""
    src = (p.root / path).read_text(errors="replace")
    h = hashlib.sha256(src.encode()).hexdigest()[:16]
    out = p.state / "digests" / _dname(path)
    if out.exists() and f"hash {h}" in out.read_text().splitlines()[1]:
        return str(out.relative_to(p.root))
    s = router.call(p, cfg, "extract", {"path": path, "source_head": src[:12000]})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"# Digest: {path}\n<!-- hash {h}, {stamp()} UTC, by tw digest -->\n\n"
                   f"{s['purpose']}\n\n"
                   + (f"- public: {', '.join(s['public_api'])}\n" if s["public_api"] else "")
                   + (f"- depends on: {', '.join(s['depends_on'])}\n" if s["depends_on"] else "")
                   + (f"- tested by: {', '.join(s['tested_by'])}\n" if s["tested_by"] else "")
                   + f"\nRead `{path}` itself only to change it.\n")
    return str(out.relative_to(p.root))


def run_tool(p: Project, name: str) -> int:
    sp = p.state / "tools" / f"{name}.sh"
    if not sp.exists():
        raise ValueError(f"no tool {name!r}: `tw tool` lists them")
    return subprocess.call(["bash", str(sp)], cwd=p.root)
