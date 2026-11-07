"""Goal and step ladder: build as you go.

goal.yaml
    goal: "plain English"
    done:            # the done bar: shell commands that must all exit 0
      - pytest -q
    out_of_scope: []

steps/NNN-<id>.yaml
    id, goal, why, changes [{path, action: edit|add|delete, what}],
    gate [shell commands], tier, budget_usd, depends_on [ids],
    status: open | locked | passed | failed

A step is LOCKED until every step it depends on (default: the one before it)
has PASSED its gate. `tw step check` runs the open step's gate; passing
records a ledger win and unlocks the next. Only the open step is worth
detailing: later ones are re-planned with what was learned (`tw adopt --refresh`).
"""

from __future__ import annotations

import re

import yaml

from . import checks, ledger, schemas
from .project import Project, ProjectError, clip, now

STATUSES = ("open", "locked", "passed", "failed")


class StepError(ProjectError):
    pass


def load_goal(p: Project) -> dict:
    if not p.goal_path.exists():
        raise StepError("goal.yaml missing: run `tw init` or write one")
    g = yaml.safe_load(p.goal_path.read_text()) or {}
    errs = []
    try:
        g["done"] = [checks.normalize(x) for x in (g.get("done") or [])]
    except checks.CheckError as e:
        errs.append(f"done: {e}")
    if not str(g.get("goal", "")).strip():
        errs.append("goal: empty")
    if not isinstance(g.get("done", []), list) or not g.get("done"):
        errs.append("done: needs at least one shell command (the measurable done bar)")
    if errs:
        raise StepError("goal.yaml invalid: " + "; ".join(errs))
    g.setdefault("out_of_scope", [])
    return g


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "step"


def load(p: Project) -> list[dict]:
    out = []
    for f in sorted(p.steps_dir.glob("[0-9][0-9][0-9]-*.yaml")):
        s = yaml.safe_load(f.read_text()) or {}
        s["gate"] = [checks.normalize(x) if isinstance(x, (str, dict)) else x for x in (s.get("gate") or [])]
        s["_file"] = str(f)
        out.append(s)
    return out


def validate_step(s: dict) -> list[str]:
    body = {k: v for k, v in s.items() if k in schemas.STEP["properties"]}
    errs = schemas.check(body, schemas.STEP, s.get("id", "step"))
    if not s.get("gate"):
        errs.append(f"{s.get('id')}: no gate: every step needs a check that proves it done")
    if s.get("status", "locked") not in STATUSES:
        errs.append(f"{s.get('id')}: bad status {s.get('status')!r}")
    return errs


def write(p: Project, steps: list[dict], keep_passed: bool = True) -> list[str]:
    """Replace the ladder. Passed steps stay (and keep their numbers); the
    first not-passed step whose dependencies passed opens."""
    p.steps_dir.mkdir(parents=True, exist_ok=True)
    old = load(p)
    passed = [s for s in old if s.get("status") == "passed"] if keep_passed else []
    passed_ids = {s["id"] for s in passed}
    for s in old:
        if s not in passed:
            from pathlib import Path
            Path(s["_file"]).unlink()
    new = [s for s in steps if s["id"] not in passed_ids]
    paths = []
    n = len(passed)
    prev = passed[-1]["id"] if passed else None
    for s in new:
        n += 1
        s = {k: v for k, v in s.items() if not k.startswith("_")}
        s.setdefault("depends_on", [prev] if prev else [])
        s["status"] = "locked"
        f = p.steps_dir / f"{n:03d}-{slug(s['id'])}.yaml"
        f.write_text(yaml.safe_dump(s, sort_keys=False, width=100))
        paths.append(str(f))
        prev = s["id"]
    refresh(p)
    return paths


def _save(s: dict) -> None:
    from pathlib import Path
    f = Path(s["_file"])
    body = {k: v for k, v in s.items() if not k.startswith("_")}
    f.write_text(yaml.safe_dump(body, sort_keys=False, width=100))


def refresh(p: Project) -> dict | None:
    """Open the first locked step whose dependencies all passed. Returns the open step."""
    steps = load(p)
    status = {s["id"]: s.get("status") for s in steps}
    opened = None
    for s in steps:
        if s.get("status") == "open":
            return s
    for s in steps:
        if s.get("status") == "locked" and all(status.get(d) == "passed" for d in s.get("depends_on", [])):
            s["status"] = "open"
            _save(s)
            opened = s
            break
    return opened


def current(p: Project) -> dict | None:
    for s in load(p):
        if s.get("status") in ("open", "failed"):
            return s
    return None


def run_gate(p: Project, items: list, timeout: float = 1800) -> list[dict]:
    """Run checks in order; stop at the first failure (cheap checks first is the author's job)."""
    out = []
    for it in items:
        r = checks.run(p, it, timeout=timeout)
        out.append(r)
        if r["rc"] != 0:
            break
    return out


def check(p: Project, step_id: str | None = None) -> dict:
    steps = load(p)
    s = next((x for x in steps if x["id"] == step_id), None) if step_id else current(p)
    if not s:
        raise StepError("no open step" if not step_id else f"no step {step_id!r}")
    if s.get("status") == "locked":
        raise StepError(f"step {s['id']} is locked: its dependencies have not passed "
                        f"({', '.join(s.get('depends_on', []))})")
    errs = validate_step(s)
    if errs:
        raise StepError("step invalid: " + "; ".join(errs))
    res = run_gate(p, s["gate"])
    ok = all(r["rc"] == 0 for r in res) and len(res) == len(s["gate"])
    s["status"] = "passed" if ok else "failed"
    _save(s)
    fail = next((r for r in res if r["rc"] != 0), None)
    ledger.record(p, "step", s["id"], "win" if ok else "fail",
                  note="gate passed" if ok else f"`{fail['cmd']}` rc {fail['rc']}: {clip(' '.join(fail['tail']), 200)}")
    nxt = refresh(p) if ok else None
    return {"step": s["id"], "pass": ok, "results": res, "next": nxt["id"] if nxt else None}


def done(p: Project) -> dict:
    g = load_goal(p)
    res = run_gate(p, g["done"])
    ok = all(r["rc"] == 0 for r in res) and len(res) == len(g["done"])
    ledger.record(p, "goal", clip(g["goal"], 60), "win" if ok else "fail",
                  note="done bar met" if ok else "done bar not met")
    return {"pass": ok, "results": res}
