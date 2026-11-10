"""Act on queue events without a model whenever a rule covers them.

For each new `end` event: build the brief, run the rule table, apply ONLY a
whitelisted action, stamp the ledger.

    ok / ignore   one ledger line
    resubmit      the job's own file back into pending as <name>_r1, once;
                  an _r1 that fails again goes to diagnose
    tune          a failed job whose trend matches a tune trigger (stall,
                  plateau below target, decay after peak) and that has a knob
                  whitelist in `tune:` gets one `tune` call (T2) instead of
                  diagnose; a checked answer is resubmitted as <name>_tN
    diagnose      attention file (.tierwright/attention/<job>.json: brief +
                  verdict). With dispatch.auto_diagnose the diagnose role (T2)
                  reads the brief and proposes ONE action; the dispatcher
                  applies it only if it is whitelisted and not a recorded dead
                  end; anything else becomes an owner-queue line.

Workers propose, code applies. Every decision is counted in the ledger, so
how often each rule fires, and what a model would have cost without it, is
measurable.
"""

from __future__ import annotations

import shutil

from . import brief as brief_mod
from . import config, events, ledger, router, rules, tune, watch
from .project import Project, owner_note, write_json

APPLY = {"resubmit", "note"}          # what a model's proposal may make code do
OWNER = {"owner", "rerun_gate"}       # recorded for a person / the step runner


def _resubmit(p: Project, job: str) -> str | None:
    if job.endswith("_r1"):
        return None
    src = p.q("done") / f"{job}.job"
    if not src.exists():
        return None
    dst = p.q("pending") / f"{job}_r1.job"
    shutil.copy(src, dst)
    events.emit(p, "submit", f"{job}_r1", retry_of=job)
    watch.copy(p, job, f"{job}_r1")
    return f"{job}_r1"


def handle(p: Project, cfg: dict, job: str) -> dict:
    b = brief_mod.build(p, job)
    v = rules.triage(b, rules.load(p))
    act, note = v["action"], v["note"]
    out = {"job": job, **v}
    if act == "resubmit":
        new = _resubmit(p, job)
        if new:
            ledger.record(p, "job", job, "unknown" if v["class"] == "preempted" else "fail",
                          note=f"{v['class']} ({v['rule']}): resubmitted as {new}")
            return {**out, "applied": f"resubmit {new}"}
        act = "diagnose"
        note = f"{v['class']}: failed again after a resubmit"
        out.update(action="diagnose", note=note)
    if act in ("ok", "ignore"):
        ledger.record(p, "job", job.removesuffix("_r1"), "ok" if act == "ok" else "same",
                      note=f"{v['rule']}: {b.get('duration_s')}s")
        if act == "ok":
            tune.outcome(p, job.removesuffix("_r1"), True, v["note"])
        return {**out, "applied": act}
    # diagnose
    att = {"brief": b, "verdict": v}
    ledger.record(p, "job", job.removesuffix("_r1"), "fail", note=f"{v['class']} ({v['rule']}): {note}")
    tune.outcome(p, job.removesuffix("_r1"), False, f"{v['class']}: {note}")
    jf = p.q("done") / f"{job}.job"
    tuned = tune.maybe(p, cfg, job, jf.read_text(), ended=True) if jf.exists() else None
    if tuned:       # tune was called: no second model call on the same failure
        att["tune"] = tuned
        write_json(p.attention / f"{job}.json", att)
        return {**out, "action": "tune", "applied": tuned}
    if cfg["dispatch"].get("auto_diagnose"):
        try:
            prop = router.call(p, cfg, "diagnose", {"brief": b, "verdict": v})
            att["diagnosis"] = prop
            out["applied"] = _apply(p, job, prop)
        except (router.BudgetExceeded, router.CallFailed) as e:
            att["diagnosis_error"] = str(e)
            out["applied"] = "attention"
    else:
        out["applied"] = "attention"
    write_json(p.attention / f"{job}.json", att)
    return out


def _apply(p: Project, job: str, prop: dict) -> str:
    pr = prop["proposal"]
    act = pr["action"]
    change = f"{act} {pr.get('target', '')}: {pr.get('change', '')}".strip()
    dead = ledger.check_proposal(p, change, parent=job.removesuffix("_r1"))
    if dead:
        owner_note(p, f"Proposal for {job} repeats a dead end", f"{change}. {dead}")
        ledger.record(p, "proposal", job, "fail", note=f"blocked: {dead}", change=change,
                      parent=job.removesuffix("_r1"))
        return "blocked (dead end)"
    if act == "resubmit":
        new = _resubmit(p, job)
        ledger.record(p, "proposal", job, "unknown", note=pr.get("why", ""), change=change,
                      parent=job.removesuffix("_r1"))
        return f"resubmit {new}" if new else "attention (already retried)"
    if act == "note":
        ledger.record(p, "proposal", job, "unknown", note=pr.get("why", ""), change=change)
        return "noted"
    owner_note(p, f"Diagnosis for {job}", f"{pr.get('why', '')} Proposed: {change}")
    ledger.record(p, "proposal", job, "unknown", note="to owner queue", change=change,
                  parent=job.removesuffix("_r1"))
    return "owner"


def poll(p: Project, cfg: dict | None = None) -> list[dict]:
    cfg = cfg or config.load(p)
    evs = events.unread(p)
    out = []
    for e in evs:
        if e.get("kind") == "end":
            out.append(handle(p, cfg, e["job"]))
    events.mark_read(p, len(evs))
    return out
