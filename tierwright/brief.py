"""Briefs, not logs: everything a rule or a model may read about one job, in a
few kilobytes of JSON. No model ever gets a raw log or a file tree."""

from __future__ import annotations

import json
import re

from . import ledger
from .project import Project, clip, write_json

ERR_RE = re.compile(r"(Traceback|Error|Exception|FAILED|FATAL|panic:|Killed|"
                    r"No such file|not found|denied|refused|timed out)", re.I)
MAX_CHARS = 8000


def build(p: Project, job: str, tail: int = 30) -> dict:
    meta_f = p.q("done") / f"{job}.json"
    if not meta_f.exists():
        raise FileNotFoundError(f"no finished job {job!r}")
    meta = json.loads(meta_f.read_text())
    errors, last = [], []
    log = meta.get("log")
    if log:
        try:
            lines = open(log, errors="replace").read().splitlines()
        except OSError:
            lines = []
        errors = [clip(l, 220) for l in lines if ERR_RE.search(l)][-15:]
        last = [clip(l, 220) for l in lines[-tail:]]
    prior = [f"{r.get('verdict')} {clip(r.get('note', ''), 100)}"
             for r in ledger.entries(p, kind="job", name=job.removesuffix("_r1"))][-5:]
    b = {
        "job": job,
        "rc": meta.get("rc"),
        "reason": meta.get("reason"),
        "duration_s": meta.get("duration_s"),
        "contract_pass": meta.get("contract", {}).get("pass"),
        "contract_failed": [f"{r['check']}: {r['evidence']}" for r in meta.get("contract", {}).get("failed", [])],
        "errors": errors,
        "tail": last,
        "prior_verdicts": prior,
        "do_not_repeat": ledger.do_not_repeat(p, limit=10),
    }
    # keep it small: trim the tail first, then errors
    while len(json.dumps(b)) > MAX_CHARS and b["tail"]:
        b["tail"] = b["tail"][1:]
    while len(json.dumps(b)) > MAX_CHARS and b["errors"]:
        b["errors"] = b["errors"][1:]
    write_json(p.briefs / f"{job}.json", b)
    return b


def text(b: dict) -> str:
    """Everything a rule may match on."""
    parts = list(b.get("errors", [])) + list(b.get("tail", [])) + list(b.get("contract_failed", []))
    parts.append(f"REASON {b.get('reason')}")
    return "\n".join(parts)
