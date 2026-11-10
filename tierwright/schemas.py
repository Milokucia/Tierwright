"""Output schemas per role, and a small validator (no dependency).

Every schema is strict (every object lists `required` and sets
`additionalProperties: false`), so the API's structured output and the
`claude -p --json-schema` path both accept it, and this validator rejects
anything else the model returns.
"""

from __future__ import annotations


def obj(props: dict, required: list | None = None) -> dict:
    return {"type": "object", "properties": props,
            "required": list(props) if required is None else required,
            "additionalProperties": False}


def arr(items: dict) -> dict:
    return {"type": "array", "items": items}


S = {"type": "string"}
N = {"type": "number"}
ACTION = {"type": "string", "enum": ["resubmit", "rerun_gate", "note", "owner"]}

CHANGE = obj({"path": S, "action": {"type": "string", "enum": ["edit", "add", "delete"]}, "what": S})
STEP = obj({
    "id": S, "goal": S, "why": S,
    "changes": arr(CHANGE),
    "gate": arr(S),
    "tier": {"type": "string", "enum": ["T0", "T1", "T2", "T3"]},
    "budget_usd": N,
    "depends_on": arr(S),
})

ROLES = {
    "triage": obj({"class": S, "action": {"type": "string", "enum": ["ignore", "resubmit", "diagnose"]},
                   "note": S}),
    "diagnose": obj({
        "hypotheses": arr(obj({"cause": S, "evidence": S,
                               "confidence": {"type": "string", "enum": ["low", "medium", "high"]}})),
        "proposal": obj({"action": ACTION, "target": S, "why": S, "change": S}),
    }),
    "review": obj({"verdict": {"type": "string", "enum": ["pass", "fail"]},
                   "findings": arr(obj({"severity": {"type": "string", "enum": ["low", "medium", "high"]},
                                        "file": S, "summary": S}))}),
    "reporter": obj({"summary": S}),
    "extract": obj({"purpose": S, "public_api": arr(S), "depends_on": arr(S), "tested_by": arr(S)}),
    "planner": obj({"summary": S, "steps": arr(STEP)}),
    "judge": obj({"verdict": {"type": "string", "enum": ["pass", "fail"]}, "reasons": arr(S)}),
    "tune": obj({"action": {"type": "string", "enum": ["change", "none"]},
                 "changes": arr(obj({"knob": S, "value": S})), "why": S}),
    "builder": obj({"status": {"type": "string", "enum": ["done", "blocked"]},
                    "changed": arr(S), "gate_ran": arr(S), "note": S}),
}

PROBE = obj({"ok": {"type": "boolean"}})


def get(role: str) -> dict:
    if role not in ROLES:
        raise KeyError(f"no schema for role {role!r}")
    return ROLES[role]


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


def check(v, schema: dict, path: str = "$") -> list[str]:
    errs: list[str] = []
    t = schema.get("type")
    if t == "number":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return [f"{path}: expected number"]
    elif t in _TYPES and not isinstance(v, _TYPES[t]):
        return [f"{path}: expected {t}"]
    if "enum" in schema and v not in schema["enum"]:
        errs.append(f"{path}: {v!r} not in {schema['enum']}")
    if t == "object":
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in v:
                errs.append(f"{path}.{k}: missing")
        if schema.get("additionalProperties") is False:
            for k in v:
                if k not in props:
                    errs.append(f"{path}.{k}: not allowed")
        for k, sub in props.items():
            if k in v:
                errs += check(v[k], sub, f"{path}.{k}")
    if t == "array":
        for i, item in enumerate(v):
            errs += check(item, schema.get("items", {}), f"{path}[{i}]")
    return errs
