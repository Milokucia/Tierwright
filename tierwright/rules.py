"""Known failures and what to do about each, as code: no model reads a job a
rule already covers.

    triage(brief, rules) -> {"rule", "class", "action", "note"}

Actions (the dispatcher's whitelist; it applies nothing else):
    ok         finished as asked
    ignore     expected, nothing to do (stopped on purpose)
    resubmit   transient: queue the same job once more (<name>_r1), never twice
    diagnose   needs judgment: attention file, and the diagnose role if enabled

Rules run in order; the first match wins. Every rule records the incident it
came from, so a rule that no longer fires can be retired with evidence. Add a
rule when the same failure is diagnosed by hand twice (`tw rule add`), and
`tw rule backtest` proves it changes no verdict it should not.

Project rules live in .tierwright/rules.yaml and run BEFORE the built-ins:
    - name: flaky-mirror
      re: "Could not resolve host: mirror.example"
      rc: 1               # optional
      action: resubmit
      class: network
      incident: "build_3 failed 2026-11-07, passed on rerun"
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import yaml

from . import brief as brief_mod
from .project import Project

ACTIONS = {"ok", "ignore", "resubmit", "diagnose"}


@dataclass
class Rule:
    name: str
    action: str
    cls: str
    incident: str
    re: str | None = None
    rc: object = "*"           # int, list of ints, or "*"
    reason: str | None = None  # match the queue's end reason
    when_pass: bool | None = None  # match contract pass/fail

    def matches(self, b: dict) -> bool:
        if self.rc != "*":
            rcs = self.rc if isinstance(self.rc, list) else [self.rc]
            if b.get("rc") not in rcs:
                return False
        if self.reason and b.get("reason") != self.reason:
            return False
        if self.when_pass is not None and bool(b.get("contract_pass")) != self.when_pass:
            return False
        if self.re and not re.search(self.re, brief_mod.text(b), re.I | re.M):
            return False
        return True


BUILTIN = [
    Rule("stopped", "ignore", "stopped", "a job removed on purpose", reason="stopped"),
    Rule("dependency-failed", "ignore", "dependency", "an upstream job failed; its own verdict covers it",
         reason="dependency_failed"),
    Rule("ok", "ok", "ok", "rc 0 and every contract check passed", rc=0, reason="exit", when_pass=True),
    Rule("preempted", "resubmit", "preempted",
         "stopped by the queue (preempted for another job) before it could finish; not a failure",
         reason="preempted"),
    Rule("network-transient", "resubmit", "network",
         "registry / mirror blips that pass on rerun",
         re=r"(Connection reset|Temporary failure in name resolution|Could not resolve host|"
            r"503 Service Unavailable|502 Bad Gateway|429 Too Many Requests|TLS handshake timeout|"
            r"ECONNRESET|ETIMEDOUT)"),
    Rule("missing-dependency", "diagnose", "setup", "a tool or module not installed",
         re=r"(ModuleNotFoundError|command not found|No module named|Cannot find module)"),
    Rule("contract-stop", "diagnose", "contract", "outputs went wrong while running",
         reason="contract_stop"),
    Rule("timeout", "diagnose", "timeout", "ran past its timeout", reason="timeout"),
    Rule("contract-fail", "diagnose", "contract", "exited but its outputs fail the contract",
         rc=0, when_pass=False),
]


def load(p: Project) -> list[Rule]:
    f = p.state / "rules.yaml"
    out = []
    if f.exists():
        for i, r in enumerate(yaml.safe_load(f.read_text()) or []):
            out.append(from_dict(r, where=f"rules.yaml #{i + 1}"))
    return out + BUILTIN


def from_dict(r: dict, where: str = "rule") -> Rule:
    for k in ("name", "action", "class", "incident"):
        if not r.get(k):
            raise ValueError(f"{where}: needs {k}")
    if r["action"] not in ACTIONS:
        raise ValueError(f"{where}: action must be one of {sorted(ACTIONS)}")
    if not (r.get("re") or "rc" in r or r.get("reason")):
        raise ValueError(f"{where}: needs at least one of re / rc / reason")
    if r.get("re"):
        re.compile(r["re"])
    return Rule(r["name"], r["action"], r["class"], r["incident"], r.get("re"),
                r.get("rc", "*"), r.get("reason"), r.get("when_pass"))


def triage(b: dict, rules: list[Rule]) -> dict:
    for r in rules:
        if r.matches(b):
            return {"rule": r.name, "class": r.cls, "action": r.action, "note": r.incident}
    return {"rule": None, "class": "unknown", "action": "diagnose", "note": "no rule matched"}


def recorded_briefs(p: Project) -> list[dict]:
    out = []
    for f in sorted(p.briefs.glob("*.json")):
        try:
            out.append(json.loads(f.read_text()))
        except json.JSONDecodeError:
            pass
    return out


def backtest(p: Project, new: Rule, incident_job: str | None = None) -> dict:
    """What adding `new` changes over every recorded brief.

    Refused if it changes the verdict of a job that finished ok, or (with
    an incident job) does not match its own incident."""
    old_rules = load(p)
    new_rules = [new] + old_rules
    changed, broke = [], []
    matched_incident = None
    for b in recorded_briefs(p):
        a, c = triage(b, old_rules), triage(b, new_rules)
        if b["job"] == incident_job:
            matched_incident = c["rule"] == new.name
        if (a["action"], a["rule"]) != (c["action"], c["rule"]):
            changed.append({"job": b["job"], "from": a["action"], "to": c["action"]})
            if a["action"] == "ok":
                broke.append(b["job"])
    errs = []
    if broke:
        errs.append(f"changes the verdict of jobs that finished ok: {broke}")
    if incident_job and matched_incident is not True:
        errs.append(f"does not match its incident job {incident_job!r}"
                    + (" (no recorded brief)" if matched_incident is None else ""))
    return {"changed": changed, "errors": errs, "ok": not errs}


def add(p: Project, r: dict, incident_job: str | None = None) -> dict:
    rule = from_dict(r)
    bt = backtest(p, rule, incident_job)
    if not bt["ok"]:
        return bt
    f = p.state / "rules.yaml"
    cur = yaml.safe_load(f.read_text()) if f.exists() else []
    cur = cur or []
    if any(x.get("name") == rule.name for x in cur):
        bt["errors"].append(f"a rule named {rule.name!r} exists")
        bt["ok"] = False
        return bt
    cur.append({k: v for k, v in r.items() if v is not None})
    f.write_text(yaml.safe_dump(cur, sort_keys=False))
    return bt
