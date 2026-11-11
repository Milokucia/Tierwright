"""Role -> tier -> backend -> model, with budgets, validation and escalation.

    call(p, cfg, role, payload, validate=None) -> output

1. Resolve the role (config.role): tier, backend, model, effort.
2. Budget: today's and this goal's spend from .tierwright/runs/. Over budget on
   the role's backend: with backend "both" the call moves to the other backend
   (a ledger line says so); otherwise it raises BudgetExceeded and an
   owner-queue line says why. Never a silent switch, never silent spend.
3. Call with a frozen prefix (roles/_common.md + roles/<role>.md: cached) and
   the payload after it, under a JSON schema.
4. Validate: schema, then the caller's `validate(output) -> [errors]`. A
   failure is retried once on the same tier with the errors appended; after
   `escalate_after_failures` failures the call moves one tier up (T1 -> T2 ->
   T3, never past `max_tier`) and the escalation is a ledger line.
5. Every attempt is a run record: role, tier, backend, model, tokens, cost,
   outcome. Cost per role and tier is what later downgrades are judged on.
"""

from __future__ import annotations

import json
import time
import uuid
from importlib import resources

from . import backends, config, ledger, schemas
from .project import Project, now, owner_note, write_json

UP = {"T1": "T2", "T2": "T3"}


class BudgetExceeded(Exception):
    pass


class CallFailed(Exception):
    pass


def role_prompt(p: Project, role: str) -> str:
    parts = []
    for name in ("_common", role):
        f = p.roles / f"{name}.md"
        if f.exists():
            parts.append(f.read_text())
        else:
            try:
                parts.append(resources.files("tierwright").joinpath(f"templates/roles/{name}.md").read_text())
            except FileNotFoundError:
                pass
    return "\n\n".join(parts).strip()


def runs(p: Project) -> list[dict]:
    out = []
    for f in sorted(p.runs.glob("*.json")):
        try:
            out.append(json.loads(f.read_text()))
        except json.JSONDecodeError:
            pass
    return out


def spend(p: Project, backend: str | None = None, since: float | None = None) -> float:
    tot = 0.0
    for r in runs(p):
        if backend and r.get("backend") != backend:
            continue
        if since and r.get("ts", 0) < since:
            continue
        tot += r.get("cost_usd", 0.0)
    return tot


def _goal_start(p: Project) -> float:
    f = p.state / "goal.started"
    return float(f.read_text()) if f.exists() else 0.0


def _day_start() -> float:
    t = time.gmtime()
    return time.mktime(time.struct_time((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, 0))) - time.timezone


def _month_start() -> float:
    t = time.gmtime()
    return time.mktime(time.struct_time((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, 0))) - time.timezone


def budget_left(p: Project, cfg: dict, backend: str) -> str | None:
    """None if `backend` may spend; else why not."""
    b = cfg["budgets"]
    if b.get("per_month_usd") is not None:       # optional: calendar month, UTC
        month = spend(p, backend, _month_start())
        if month >= b["per_month_usd"]:
            return f"{backend} spent ${month:.2f} this month of ${b['per_month_usd']}"
    day = spend(p, backend, _day_start())
    goal = spend(p, backend, _goal_start(p))
    if day >= b["per_day_usd"]:
        return f"{backend} spent ${day:.2f} today of ${b['per_day_usd']}"
    if goal >= b["per_goal_usd"]:
        return f"{backend} spent ${goal:.2f} on this goal of ${b['per_goal_usd']}"
    return None


def _pick_backend(p: Project, cfg: dict, r: dict) -> str:
    why = budget_left(p, cfg, r["backend"])
    if not why:
        return r["backend"]
    if cfg["backend"] == "both":
        other = "api" if r["backend"] == "subscription" else "subscription"
        why2 = budget_left(p, cfg, other)
        if not why2:
            ledger.record(p, "escalation", r["role"], "unknown",
                          note=f"budget: {why}; moved to {other}")
            return other
        why = f"{why}; {why2}"
    owner_note(p, f"Budget reached for role {r['role']}",
               f"{why}. Raise budgets in tierwright.yaml or wait for tomorrow.")
    raise BudgetExceeded(why)


def call(p: Project, cfg: dict, role: str, payload, validate=None, tools=(), cwd=None,
         tier: str | None = None, max_tier: str | None = None):
    p.ensure()
    r = config.role(cfg, role)
    if tier:
        r = {**r, "tier": tier, **({"model": cfg["tiers"][tier]["model"],
                                     "effort": cfg["tiers"][tier].get("effort", "medium")}
                                    if tier != "T0" else {})}
    if r["tier"] == "T0":
        raise CallFailed(f"role {role} is T0 (code only): no model call")
    schema = schemas.get(role)
    system = role_prompt(p, role)
    base_user = payload if isinstance(payload, str) else json.dumps(payload, indent=1, sort_keys=True)
    fails_on_tier, errors_seen = 0, []
    limit = int(cfg["limits"]["escalate_after_failures"])
    while True:
        backend_name = _pick_backend(p, cfg, r)
        be = backends.get(backend_name)
        user = base_user
        if errors_seen:
            user += ("\n\nYOUR PREVIOUS ANSWER WAS REJECTED. Fix exactly these and answer again:\n- "
                     + "\n- ".join(errors_seen[-6:]))
        t0 = now()
        res = be.call(model=r["model"], effort=r["effort"], system=system, user=user,
                      schema=schema, timeout=cfg["limits"]["call_timeout_s"], tools=tools,
                      cwd=cwd, max_budget_usd=cfg["budgets"]["per_call_usd"],
                      pricing=cfg.get("pricing"), role=role)
        errs = [res.error] if res.error else []
        if not errs:
            errs = schemas.check(res.output, schema)
        if not errs and validate:
            errs = list(validate(res.output) or [])
        rec = {"id": uuid.uuid4().hex[:12], "ts": t0, "duration_s": round(now() - t0, 2),
               "role": role, "tier": r["tier"], "backend": be.name, "model": r["model"],
               "effort": r["effort"], "usage": res.usage, "cost_usd": round(res.cost_usd, 6),
               "outcome": "ok" if not errs else "rejected", "errors": errs[:6],
               "input_chars": len(system) + len(user)}
        write_json(p.runs / f"{int(t0)}-{rec['id']}.json", rec)
        if not errs:
            return res.output
        errors_seen += errs
        fails_on_tier += 1
        if fails_on_tier >= limit:
            nxt = UP.get(r["tier"])
            if nxt and max_tier and int(nxt[1]) > int(max_tier[1]):
                nxt = None
            if not nxt:
                ledger.record(p, "escalation", role, "fail",
                              note=f"failed on top tier: {errs[0]}")
                raise CallFailed(f"{role} failed on {r['tier']} after {fails_on_tier} tries: {errs[:3]}")
            ledger.record(p, "escalation", role, "unknown",
                          note=f"{r['tier']} -> {nxt} after {fails_on_tier} rejected answers: {errs[0]}")
            r = {**r, "tier": nxt, "model": cfg["tiers"][nxt]["model"],
                 "effort": cfg["tiers"][nxt].get("effort", "medium")}
            fails_on_tier = 0


def summary(p: Project, since: float | None = None) -> dict:
    """Spend and outcomes by role / tier / backend."""
    out: dict = {}
    for r in runs(p):
        if since and r.get("ts", 0) < since:
            continue
        k = f"{r['role']} {r['tier']} {r['backend']}"
        s = out.setdefault(k, {"calls": 0, "rejected": 0, "cost_usd": 0.0, "in": 0, "out": 0})
        s["calls"] += 1
        s["rejected"] += r.get("outcome") != "ok"
        s["cost_usd"] += r.get("cost_usd", 0.0)
        s["in"] += r.get("usage", {}).get("input_tokens", 0)
        s["out"] += r.get("usage", {}).get("output_tokens", 0)
    return out


def probe(p: Project, cfg: dict, backend_name: str) -> str | None:
    """One tiny call on the cheapest tier: None if the backend works."""
    be = backends.get(backend_name)
    why = be.available()
    if why:
        return why
    model = cfg["tiers"]["T1"]["model"]
    kw = dict(model=model, effort="low", system="Answer with JSON only.",
              user='Reply {"ok": true}', schema=schemas.PROBE, timeout=120,
              pricing=cfg.get("pricing"))
    res = be.call(role="probe", **kw)
    write_json(p.runs / f"{int(now())}-probe-{backend_name}.json",
               {"id": "probe", "ts": now(), "role": "probe", "tier": "T1", "backend": backend_name,
                "model": model, "usage": res.usage, "cost_usd": res.cost_usd,
                "outcome": "ok" if not res.error else "rejected", "errors": [res.error] if res.error else []})
    return res.error

