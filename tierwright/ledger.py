"""The ledger: every attempt and its verdict. A result in no ledger did not happen.

Records (one JSON per line in .tierwright/ledger.jsonl):
    ts, kind (step | job | proposal | rule | escalation | plan | note),
    name, change, parent, verdict (win | fail | same | unknown | ok),
    note, and anything else the writer adds.

`do_not_repeat()` lists changes that already failed on the same parent; every
brief and every planner prompt carries it, and `check_proposal()` refuses a
proposal that repeats one.
"""

from __future__ import annotations

import json

from .project import Project, append_jsonl, clip, now, read_jsonl, stamp

VERDICTS = {"win", "fail", "same", "unknown", "ok"}


def record(p: Project, kind: str, name: str, verdict: str, note: str = "",
           change: str = "", parent: str = "", **extra) -> dict:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(VERDICTS)}")
    rec = {"ts": now(), "kind": kind, "name": name, "verdict": verdict,
           "note": clip(note, 400), "change": change, "parent": parent, **extra}
    append_jsonl(p.ledger, rec)
    return rec


def entries(p: Project, kind: str | None = None, name: str | None = None) -> list[dict]:
    out = read_jsonl(p.ledger)
    if kind:
        out = [r for r in out if r.get("kind") == kind]
    if name:
        out = [r for r in out if r.get("name") == name]
    return out


def _key(change, parent) -> str:
    if not isinstance(change, str):
        change = json.dumps(change, sort_keys=True)
    return f"{parent}::{' '.join(change.split()).lower()}"


def do_not_repeat(p: Project, limit: int = 30) -> list[str]:
    seen, out = set(), []
    for r in reversed(read_jsonl(p.ledger)):
        if r.get("verdict") == "fail" and r.get("change"):
            k = _key(r["change"], r.get("parent", ""))
            if k not in seen:
                seen.add(k)
                out.append(f"{r.get('kind')} {r.get('name')}: {clip(r['change'], 120)}"
                           + (f" (on {r['parent']})" if r.get("parent") else ""))
        if len(out) >= limit:
            break
    return out


def check_proposal(p: Project, change, parent: str = "") -> str | None:
    """None if the change is new; else the ledger note of the failure it repeats."""
    k = _key(change, parent)
    for r in read_jsonl(p.ledger):
        if r.get("verdict") == "fail" and r.get("change") and _key(r["change"], r.get("parent", "")) == k:
            return f"repeats a failed {r.get('kind')} {r.get('name')} at {stamp(r.get('ts'))}: {r.get('note')}"
    return None


def lines(p: Project, n: int = 10) -> list[str]:
    out = []
    for r in read_jsonl(p.ledger)[-n:]:
        out.append(f"{stamp(r.get('ts'))} {r.get('kind')} {r.get('name')} {r.get('verdict').upper()} "
                   f"{clip(r.get('note', ''), 90)}")
    return out
