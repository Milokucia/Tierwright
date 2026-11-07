"""`tw adopt`: read a codebase, output the exact steps from what exists to the goal.

Cheapest first:
  1. inventory (T0)   languages, manifests, test command, CI, agent files, churn
  2. fit (T0)         which Tierwright components already exist, with the file
  3. repo map (T1)    one short summary per module, cached by content hash;
                      a re-run summarizes only changed files
  4. gap plan (T3)    one planner call: goal + inventory + fit + map +
                      do-not-repeat, never raw source
  5. validate (T0)    paths, gate commands, order, tiers, protected paths,
                      dead ends; a failing plan is retried, then escalated;
                      the user never sees an unvalidated plan
  6. write            steps/NNN-*.yaml and GAP.md

With --no-model, steps 3-4 are skipped and the plan is built from the fit
table alone (T0): one step per missing component, still validated.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from collections import Counter
from pathlib import Path

from . import ledger, router, steps
from .project import Project, clip, stamp, write_json

SKIP_DIRS = {".git", ".tierwright", "node_modules", "__pycache__", ".venv", "venv", "dist",
             "build", ".mypy_cache", ".pytest_cache", "target", ".idea", ".vscode", ".tox"}
LANG = {".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
        ".jsx": "javascript", ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin",
        ".rb": "ruby", ".cs": "csharp", ".php": "php", ".c": "c", ".h": "c", ".cpp": "cpp",
        ".hpp": "cpp", ".swift": "swift", ".sh": "shell", ".md": "markdown"}
MANIFESTS = ["pyproject.toml", "setup.py", "requirements.txt", "package.json", "Cargo.toml",
             "go.mod", "pom.xml", "build.gradle", "Gemfile", "composer.json", "Makefile",
             "Dockerfile", "docker-compose.yml", ".devcontainer/devcontainer.json"]
MAX_FILES = 20000
# Files Tierwright itself writes: never evidence about the user's codebase.
GENERATED = ("tierwright.yaml", "goal.yaml", "GAP.md", "STATUS.md", "HANDOFF.md", "owner-queue.md",
             ".claude/agents/tw-", "steps/0", "steps/1", "steps/2", "steps/3", "steps/4", "steps/5",
             "steps/6", "steps/7", "steps/8", "steps/9")


def _generated(f: str) -> bool:
    return f.startswith(GENERATED)


def files(root: Path) -> list[str]:
    """Tracked + untracked-not-ignored files when in git; else a walk."""
    try:
        pr = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=root,
                            capture_output=True, text=True, timeout=60)
        if pr.returncode == 0:
            out = [f for f in pr.stdout.splitlines()
                   if f and not _generated(f) and not any(part in SKIP_DIRS for part in Path(f).parts)]
            return out[:MAX_FILES]
    except (OSError, subprocess.TimeoutExpired):
        pass
    out = []
    for d, dirs, fs in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in fs:
            rel = os.path.relpath(os.path.join(d, f), root)
            if _generated(rel):
                continue
            out.append(rel)
            if len(out) >= MAX_FILES:
                return out
    return out


def _head(path: Path, n: int = 64 * 1024) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(n).decode("utf-8", "replace")
    except OSError:
        return ""


def test_command(root: Path, fl: set[str]) -> str | None:
    if "package.json" in fl:
        try:
            if json.loads((root / "package.json").read_text()).get("scripts", {}).get("test"):
                return "npm test --silent"
        except (OSError, json.JSONDecodeError):
            pass
    if "Cargo.toml" in fl:
        return "cargo test -q"
    if "go.mod" in fl:
        return "go test ./..."
    if any(f.endswith(".py") for f in fl) and any(
            Path(f).name.startswith("test_") or "/tests/" in f"/{f}" for f in fl):
        return "python3 -m pytest -q"
    if "Makefile" in fl and re.search(r"^test:", _head(root / "Makefile"), re.M):
        return "make test"
    return None


def inventory(root: Path) -> dict:
    fl = files(root)
    fs = set(fl)
    langs = Counter(LANG[Path(f).suffix] for f in fl if Path(f).suffix in LANG)
    todo = 0
    for f in fl[:3000]:
        if Path(f).suffix in LANG and Path(f).suffix != ".md":
            todo += len(re.findall(r"\b(TODO|FIXME|XXX)\b", _head(root / f, 32 * 1024)))
    top = Counter(Path(f).parts[0] if len(Path(f).parts) > 1 else "." for f in fl)
    churn = {}
    try:
        pr = subprocess.run(["git", "log", "-n", "200", "--name-only", "--format="], cwd=root,
                            capture_output=True, text=True, timeout=60)
        if pr.returncode == 0:
            churn = dict(Counter(Path(f).parts[0] if len(Path(f).parts) > 1 else "."
                                 for f in pr.stdout.splitlines() if f).most_common(10))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "files": len(fl),
        "languages": dict(langs.most_common(8)),
        "manifests": [m for m in MANIFESTS if m in fs],
        "test_command": test_command(root, fs),
        "ci": sorted(f for f in fl if f.startswith(".github/workflows/") or f in (".gitlab-ci.yml",
                     ".circleci/config.yml")),
        "agent_files": sorted(f for f in fl if f in ("CLAUDE.md", "AGENTS.md") or f.endswith("/CLAUDE.md")
                              or f.startswith(".claude/")),
        "top_dirs": dict(top.most_common(12)),
        "churn_last_200_commits": churn,
        "todo_fixme": todo,
    }


# Components of the Tierwright approach. `provided`: Tierwright supplies it once
# initialized, so a missing one is not a step; an existing one of the user's own
# is listed so the plan can integrate it instead of duplicating it.
COMPONENTS = {
    "tests": ("fast test suite", None, False),
    "ci": ("CI running the tests", None, False),
    "contract": ("output contracts on long jobs", r"#@ (artifact|result|forbid|progress|success|phase) ", True),
    "queue": ("job queue with timeouts", r"(gpu_queue|job queue|celery|rq\.Queue|bullmq|\bsbatch\b)", True),
    "rules": ("triage rule table for known failures", r"(triage_rules|def triage\()", True),
    "ledger": ("attempts ledger with do-not-repeat", r"(attempts\.jsonl|[Dd]o not repeat)", True),
    "router": ("model per agent role", r"^model:\s*(haiku|sonnet|opus)", True),
    "handoff": ("session handoff doc", None, True),
}
CODE_EXT = (".py", ".ts", ".js", ".sh", ".go", ".rs", ".yaml", ".yml", ".json", ".job", ".toml")


def fit(root: Path, inv: dict | None = None) -> dict:
    inv = inv or inventory(root)
    fl = files(root)
    out = {}
    for name, (what, pat, provided) in COMPONENTS.items():
        hits = []
        if pat:
            rx = re.compile(pat, re.M)
            pool = [f for f in fl if f.startswith(".claude/agents/")] if name == "router" else \
                [f for f in fl if Path(f).suffix in CODE_EXT]
            for f in pool[:4000]:
                if rx.search(_head(root / f, 128 * 1024)):
                    hits.append(f)
                    if len(hits) >= 5:
                        break
        elif name == "handoff":
            hits = [f for f in fl if re.search(r"(^|/)HANDOFF[^/]*\.md$", f)][:5]
        elif name == "tests":
            hits = [inv["test_command"]] if inv.get("test_command") else []
        elif name == "ci":
            hits = inv.get("ci", [])[:5]
        if hits:
            status = "present"
        else:
            status = "tierwright" if provided else "missing"
        out[name] = {"what": what, "status": status, "files": hits}
    return out


# Repo map ---------------------------------------------------------------------
def modules(root: Path, limit: int = 60) -> list[str]:
    fl = [f for f in files(root) if Path(f).suffix in LANG and Path(f).suffix != ".md"]
    fl = [f for f in fl if not re.search(r"(^|/)(test_|tests?/|fixtures?/)", f)]
    fl.sort(key=lambda f: (-(root / f).stat().st_size if (root / f).exists() else 0))
    return sorted(fl[:limit])


def repo_map(p: Project, cfg: dict, use_model: bool, limit: int = 60) -> dict:
    cache_f = p.state / "map.json"
    cache = json.loads(cache_f.read_text()) if cache_f.exists() else {}
    out, n_new = {}, 0
    for f in modules(p.root, limit):
        src = _head(p.root / f, 24 * 1024)
        h = hashlib.sha256(src.encode()).hexdigest()[:16]
        if f in cache and cache[f].get("hash") == h:
            out[f] = cache[f]
            continue
        entry = {"hash": h, "lines": src.count("\n")}
        defs = re.findall(r"^(?:def|class|func|fn|export (?:function|class|const))\s+(\w+)", src, re.M)
        entry["defs"] = defs[:15]
        if use_model:
            try:
                entry["summary"] = router.call(p, cfg, "extract", {"path": f, "source_head": src[:6000]})
                n_new += 1
            except (router.BudgetExceeded, router.CallFailed) as e:
                entry["summary_error"] = str(e)[:200]
        out[f] = entry
    write_json(cache_f, out)
    return {"modules": out, "summarized_now": n_new}


# Plan -------------------------------------------------------------------------
def _gate_resolves(root: Path, cmd: str, planned_adds: set[str] | None = None) -> str | None:
    try:
        toks = shlex.split(cmd)
    except ValueError as e:
        return f"cannot parse: {e}"
    toks = [t for t in toks if not re.match(r"^\w+=", t)]  # env assignments
    if not toks:
        return "empty command"
    exe = toks[0]
    if exe in ("cd", "test", "[", "true", "echo", "grep", "ls", "cat", "bash", "sh"):
        return None
    if "/" in exe:
        rel = exe.lstrip("./")
        if (root / exe).exists() or (planned_adds and rel in planned_adds):
            return None
        return f"{exe} does not exist"
    if shutil.which(exe) is None:
        return f"`{exe}` not on PATH"
    return None


def validate_plan(p: Project, cfg: dict, plan: dict) -> list[str]:
    errs = []
    st = plan.get("steps", [])
    if not st:
        return ["plan has no steps"]
    ids = [s.get("id") for s in st]
    if len(set(ids)) != len(ids):
        errs.append(f"duplicate step ids: {[i for i in ids if ids.count(i) > 1]}")
    protected = cfg.get("hooks", {}).get("protected_paths", [])
    seen: set[str] = set()
    adds: set[str] = set()
    passed = {s["id"] for s in steps.load(p) if s.get("status") == "passed"}
    for s in st:
        sid = s.get("id", "?")
        errs += steps.validate_step({**s, "status": "locked"})
        for d in s.get("depends_on", []):
            if d not in seen and d not in passed:
                errs.append(f"{sid}: depends on {d!r}, which is not an earlier step")
        for ch in s.get("changes", []):
            path, act = ch.get("path", ""), ch.get("action")
            exists = (p.root / path).exists() or path in adds
            if act in ("edit", "delete") and not exists:
                errs.append(f"{sid}: {act} {path}: file does not exist")
            if act == "add" and (p.root / path).exists():
                errs.append(f"{sid}: add {path}: already exists (use edit)")
            if act == "add":
                adds.add(path)
            if any(fnmatch.fnmatch(path, g) for g in protected):
                errs.append(f"{sid}: touches protected path {path} (owner decision, not a step)")
        for g in s.get("gate", []):
            why = _gate_resolves(p.root, g, adds)
            if why:
                errs.append(f"{sid}: gate `{g}`: {why}")
        dead = ledger.check_proposal(p, s.get("goal", ""), "")
        if dead:
            errs.append(f"{sid}: {dead}")
        seen.add(sid)
    return errs


FIT_STEPS = {
    "tests": ("Add a fast test suite", "Every later step needs a gate; a gate needs tests.",
              [{"path": "tests/test_smoke.py", "action": "add", "what": "one import/smoke test per package"}],
              ["python3 -m pytest -q"], "T2"),
    "ci": ("Run the tests in CI", "A gate nobody runs is not a gate.",
           [{"path": ".github/workflows/test.yml", "action": "add", "what": "run the test command on push"}],
           ["test -f .github/workflows/test.yml"], "T1"),
}


def t0_plan(p: Project, fitted: dict, inv: dict) -> dict:
    """Start where the goal is: the goal step first, gated by the done bar;
    then only the hardening the fit table shows missing."""
    g = steps.load_goal(p)
    out = [{"id": "goal", "goal": f"Meet the done bar: {clip(g['goal'], 80)}",
            "why": "Start where the goal is. Its gate is the done bar; detail its changes with `tw adopt` "
                   "(planner) or build it directly.",
            "changes": [], "gate": list(g["done"]), "tier": "T3", "budget_usd": 10.0, "depends_on": []}]
    for name in ("tests", "ci"):
        if fitted.get(name, {}).get("status") != "missing":
            continue
        goal, why, changes, gate, tier = FIT_STEPS[name]
        out.append({"id": name, "goal": goal, "why": why, "changes": changes, "gate": gate,
                    "tier": tier, "budget_usd": 0.5 if tier == "T1" else 2.0, "depends_on": []})
    prev = None
    for s in out:
        s["depends_on"] = [prev] if prev else []
        prev = s["id"]
    return {"summary": "Built from the fit table only (no model call).", "steps": out}


def plan(p: Project, cfg: dict, use_model: bool = True, map_limit: int = 60, refresh: bool = False) -> dict:
    p.ensure()
    g = steps.load_goal(p)
    inv = inventory(p.root)
    write_json(p.state / "inventory.json", inv)
    fitted = fit(p.root, inv)
    passed = [s for s in steps.load(p) if s.get("status") == "passed"]
    if not use_model:
        pl = t0_plan(p, fitted, inv)
        errs = validate_plan(p, cfg, pl)
        if errs:  # drop gates a T0 plan cannot know yet rather than show a broken plan
            pl["steps"] = [s for s in pl["steps"] if not any(e.startswith(s["id"] + ":") for e in errs)]
        source = "T0"
        rmap = {"modules": {}, "summarized_now": 0}
    else:
        rmap = repo_map(p, cfg, use_model=True, limit=map_limit)
        payload = {
            "goal": g,
            "inventory": inv,
            "fit": fitted,
            "repo_map": {f: {k: v for k, v in m.items() if k in ("summary", "defs", "lines")}
                         for f, m in rmap["modules"].items()},
            "passed_steps": [{"id": s["id"], "goal": s.get("goal")} for s in passed],
            "do_not_repeat": ledger.do_not_repeat(p),
            "rules": "Every step: concrete file changes, a gate of shell commands that exit 0 only when "
                     "the step is done, the lowest tier that can do it, depends_on earlier step ids only. "
                     "Detail the first 3 steps; later steps may be one line with one gate.",
        }
        pl = router.call(p, cfg, "planner", payload, validate=lambda o: validate_plan(p, cfg, o))
        source = "planner"
    paths = steps.write(p, pl["steps"], keep_passed=True)
    ledger.record(p, "plan", "refresh" if refresh else "adopt", "unknown",
                  note=f"{len(pl['steps'])} steps from {source}; {sum(1 for v in fitted.values() if v['status'] != 'missing')}"
                       f"/{len(fitted)} components covered")
    gap = render_gap(p, g, inv, fitted, pl, source, rmap)
    (p.root / "GAP.md").write_text(gap)
    return {"steps": paths, "source": source, "fit": fitted, "inventory": inv,
            "summarized_now": rmap["summarized_now"]}


def _est_tokens(p: Project, tier: str) -> str:
    rs = [r for r in router.runs(p) if r.get("tier") == tier and r.get("outcome") == "ok"]
    if not rs:
        return "-"
    avg = sum(r.get("usage", {}).get("input_tokens", 0) + r.get("usage", {}).get("output_tokens", 0)
              for r in rs) / len(rs)
    return f"~{avg / 1000:.0f}k/call"


def render_gap(p, g, inv, fitted, pl, source, rmap) -> str:
    L = [f"# GAP: {clip(g['goal'], 100)}", "",
         f"Written by `tw adopt` {stamp()} UTC (plan source: {source}). Regenerate with `tw adopt --refresh`.", "",
         "## What exists", "",
         f"- {inv['files']} files; languages: " + ", ".join(f"{k} {v}" for k, v in inv["languages"].items()),
         f"- manifests: {', '.join(inv['manifests']) or 'none'}; tests: `{inv['test_command'] or 'none found'}`;"
         f" CI: {', '.join(inv['ci']) or 'none'}",
         f"- agent files: {', '.join(inv['agent_files']) or 'none'}",
         "", "## Tierwright fit", "", "| Component | Status | Where |", "|---|---|---|"]
    for k, v in fitted.items():
        L.append(f"| {k} ({v['what']}) | {v['status']} | {', '.join(v['files'][:3]) or '-'} |")
    L += ["", "## Steps", "", pl.get("summary", ""), ""]
    for i, s in enumerate(pl["steps"], 1):
        L.append(f"### {i}. {s['id']}: {s['goal']} ({s['tier']}, ${s.get('budget_usd', 0)}, est. {_est_tokens(p, s['tier'])})")
        L.append("")
        L.append(s.get("why", ""))
        L.append("")
        for ch in s.get("changes", []):
            L.append(f"- `{ch['action']}` `{ch['path']}`: {ch['what']}")
        L.append("- gate: " + "; ".join(f"`{c}`" for c in s.get("gate", [])))
        L.append("")
    dnr = ledger.do_not_repeat(p)
    if dnr:
        L += ["## Do not repeat", ""] + [f"- {x}" for x in dnr] + [""]
    return "\n".join(L)
