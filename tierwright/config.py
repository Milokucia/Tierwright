"""`tierwright.yaml`: backend choice, budgets, tiers, role overrides, hooks.

Validated on every load; a bad config fails loudly, never half-applies.
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path

import yaml

from .project import Project, ProjectError

BACKENDS = ("subscription", "api", "both")
MODES = ("full", "lite")
TIERS = ("T0", "T1", "T2", "T3")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Roles and their default tier. T0 roles never call a model.
DEFAULT_ROLES = {
    "triage":   {"tier": "T1"},   # classify a failure no rule matched
    "extract":  {"tier": "T1"},   # repo-map summaries, field extraction
    "reporter": {"tier": "T1"},   # one-line prose for status / handoff
    "judge":    {"tier": "T1"},   # pass/fail a document against a guide (`review:` checks)
    "diagnose": {"tier": "T2"},   # ranked hypotheses + one probe
    "review":   {"tier": "T2"},   # milestone / code-area review
    "planner":  {"tier": "T3"},   # goal + codebase -> step ladder
    "builder":  {"tier": "T3"},   # build a step in a worktree
    "tune":     {"tier": "T2"},   # trend + knob whitelist -> one knob change for a live run
}

# With backend "both": which backend each role uses unless overridden.
# High-volume / batchable roles go to the API; long interactive builds to the plan.
BOTH_SPLIT = {"triage": "api", "extract": "api", "reporter": "api", "review": "api", "judge": "api",
              "tune": "api", "diagnose": "subscription", "planner": "subscription", "builder": "subscription"}

DEFAULTS = {
    "version": 1,
    "mode": "full",
    "backend": "subscription",
    "budgets": {"per_call_usd": 1.0, "per_day_usd": 10.0, "per_goal_usd": 50.0},
    "tiers": {
        "T1": {"model": "claude-haiku-5-5", "effort": "low"},
        "T2": {"model": "claude-sonnet-5-5", "effort": "medium"},
        "T3": {"model": "claude-opus-5-5", "effort": "high"},
    },
    # $ per 1M tokens (input, output); used to price API calls and estimate plan calls.
    "pricing": {
        "claude-haiku-5-5": [0.10, 0.50],
        "claude-sonnet-5-5": [2.00, 10.00],
        "claude-opus-5-5": [4.00, 20.00],
    },
    "roles": {},
    "limits": {"call_timeout_s": 900, "escalate_after_failures": 2, "job_timeout_s": 3600},
    "dispatch": {"auto_diagnose": True},
    # Live-run tuning (tune.py): off until jobs and their knob whitelists are listed.
    "tune": {"enabled": False, "max_per_job": 2, "max_per_day": 6, "points": 20, "max_tier": "T3",
             "triggers": {"stall": True, "plateau": {"window": 5, "eps": 0.01},
                          "decay": {"drop": 0.1, "min_points": 2}},
             "jobs": []},
    # `tw watch` alerts: Pushover credentials from these env vars, else KEY=VALUE lines in file.
    "notify": {"pushover": {"token_env": "PUSHOVER_TOKEN", "user_env": "PUSHOVER_USER",
                            "file": "~/.pushover"}},
    "agents": {"push": "never"},   # never | after_gate: whether builder/task agents push
    "hooks": {"block_polling": True, "block_raw_logs": True, "protected_paths": []},
    # Contract dialect accepted by `tw submit` / `tw contract lint` (see contract.py).
    "contract": {"unknown": "auto", "kinds": [], "opts": {}, "flags": []},
    # Where jobs run: local (this machine) or ssh (a remote queue that runs and
    # verifies them; see remote.py). Paths are on the remote host.
    "queue": {
        "backend": "local",
        "host": None,
        "key": None,
        "pending_dir": "~/gq/pending",
        "running_dir": "~/gq/running",
        "done_dir": "~/gq/done",
        "ledger": "~/fill_ledger.txt",
        "submit_cmd": None,       # local command that tags, lints and copies a job instead of scp
        "connect_timeout_s": 15,
        "poll_s": 30,             # `tw queue run --watch` pull interval
        # ledger line -> event; each needs a (?P<job>...) group, rc= is read from (?P<rc>...)
        "events": {
            "start": r"GQ start (?P<job>\S+?)(?:\.job)? on GPU (?P<gpu>\d+)",
            "end": r"GQ end (?P<job>\S+?)(?:\.job)? rc=(?P<rc>-?\d+)",
            "stop": r"GQ HEALTH stop (?P<job>\S+?)(?:\.job)?: (?P<detail>.*)",
            "pass": r"GQ CONTRACT PASS (?P<job>\S+?)(?:\.job)? rc=(?P<rc>-?\d+)",
            "fail": r"GQ CONTRACT FAIL (?P<job>\S+?)(?:\.job)? rc=(?P<rc>-?\d+): (?P<detail>.*)",
            # stopped by the queue (preempted) or a coordinator: not judged, resubmitted once
            "skip": r"GQ CONTRACT SKIP (?P<job>\S+?)(?:\.job)? rc=(?P<rc>-?\S+): (?P<detail>.*)",
        },
        # Tierwright-only #@ lines -> the remote queue's header lines (null = strip, with a
        # warning). {hours}/{minutes}/{seconds} for timeout, {job} for after.
        "translate": {
            "timeout": "# MAX_H={hours}",
            "after": "# AFTER=~/gq/marks/{job}.ok",
        },
        "header_lines": 3,        # the remote queue reads headers from this many first lines
        # each running job's stdout on the host, read for its trend (null: ledger lines only)
        # job-name globs tracked without `tw submit` (jobs the host's own scripts queue):
        # null = the tune.jobs match patterns; [] = none
        "adopt": None,
        "log_dir": "~/gq/logs",
        "log_name": "{job}.job.log",
    },
}

PUSH_POLICIES = ("never", "after_gate")
QUEUE_BACKENDS = ("local", "ssh")
QUEUE_EVENTS = ("start", "end", "stop", "pass", "fail", "skip")
TRANSLATE = {"timeout": ("{hours}", "{minutes}", "{seconds}"), "after": ("{job}",)}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class ConfigError(ProjectError):
    pass


def validate(cfg: dict) -> list[str]:
    errs = []
    if cfg.get("mode") not in MODES:
        errs.append(f"mode must be one of {MODES}, got {cfg.get('mode')!r}")
    if cfg.get("backend") not in BACKENDS:
        errs.append(f"backend must be one of {BACKENDS}, got {cfg.get('backend')!r}")
    for k in ("per_call_usd", "per_day_usd", "per_goal_usd"):
        v = cfg.get("budgets", {}).get(k)
        if not isinstance(v, (int, float)) or v < 0:
            errs.append(f"budgets.{k} must be a number >= 0")
    for t in ("T1", "T2", "T3"):
        tc = cfg.get("tiers", {}).get(t)
        if not tc or not tc.get("model"):
            errs.append(f"tiers.{t}.model missing")
        elif tc.get("effort", "medium") not in EFFORTS:
            errs.append(f"tiers.{t}.effort must be one of {EFFORTS}")
    for name, rc in (cfg.get("roles") or {}).items():
        if rc.get("tier") and rc["tier"] not in TIERS:
            errs.append(f"roles.{name}.tier must be one of {TIERS}")
        if rc.get("backend") and rc["backend"] not in ("subscription", "api"):
            errs.append(f"roles.{name}.backend must be subscription or api")
        if rc.get("backend") and cfg.get("backend") != "both" and rc["backend"] != cfg.get("backend"):
            errs.append(f"roles.{name}.backend={rc['backend']} but backend is {cfg.get('backend')}")
    if (cfg.get("agents") or {}).get("push") not in PUSH_POLICIES:
        errs.append(f"agents.push must be one of {PUSH_POLICIES}, got {(cfg.get('agents') or {}).get('push')!r}")
    errs += _validate_contract(cfg.get("contract") or {})
    errs += _validate_queue(cfg.get("queue") or {})
    from . import tune
    errs += tune.validate_cfg(cfg.get("tune") or {})
    return errs


def _validate_contract(c: dict) -> list[str]:
    errs = []
    if c.get("unknown") not in ("auto", "error", "warn"):
        errs.append("contract.unknown must be auto, error or warn")
    for k in ("kinds", "flags"):
        if not isinstance(c.get(k), list) or not all(isinstance(x, str) for x in c[k]):
            errs.append(f"contract.{k} must be a list of words")
    o = c.get("opts")
    if not isinstance(o, dict) or not all(isinstance(v, list) for v in o.values()):
        errs.append("contract.opts must map a check kind to a list of option names")
    return errs


def _validate_queue(q: dict) -> list[str]:
    errs = []
    if q.get("backend") not in QUEUE_BACKENDS:
        return [f"queue.backend must be one of {QUEUE_BACKENDS}, got {q.get('backend')!r}"]
    for k in ("connect_timeout_s", "poll_s"):
        if not isinstance(q.get(k), (int, float)) or q[k] <= 0:
            errs.append(f"queue.{k} must be a number > 0")
    if q["backend"] == "local":
        return errs
    host = q.get("host")
    if not isinstance(host, str) or not re.fullmatch(r"[\w.@:\[\]-]+", host):
        errs.append(f"queue.host must be [user@]host for backend ssh, got {host!r}")
    if q.get("key") and not os.path.isfile(os.path.expanduser(str(q["key"]))):
        errs.append(f"queue.key {q['key']} does not exist")
    for k in ("pending_dir", "running_dir", "ledger"):
        if not isinstance(q.get(k), str) or not q[k].strip():
            errs.append(f"queue.{k} must be a path on the remote host")
    if q.get("submit_cmd") is not None and not isinstance(q["submit_cmd"], str):
        errs.append("queue.submit_cmd must be a command string or null")
    ev = q.get("events") or {}
    for k in QUEUE_EVENTS:
        r = ev.get(k)
        if r is None and k in ("start", "end", "stop", "skip"):
            continue
        try:
            rx = re.compile(r)
        except (re.error, TypeError) as e:
            errs.append(f"queue.events.{k}: bad regex {r!r}: {e}")
            continue
        if "job" not in rx.groupindex:
            errs.append(f"queue.events.{k} needs a (?P<job>...) group")
    for k in set(ev) - set(QUEUE_EVENTS):
        errs.append(f"queue.events.{k}: unknown event (one of {QUEUE_EVENTS})")
    tr = q.get("translate")
    if not isinstance(tr, dict):
        errs.append("queue.translate must map timeout / after to a header line or null")
        tr = {}
    for k, v in tr.items():
        if k not in TRANSLATE:
            errs.append(f"queue.translate.{k}: only {sorted(TRANSLATE)} can be translated")
        elif v is not None and (not isinstance(v, str) or "\n" in v
                                or not any(f in v for f in TRANSLATE[k])):
            errs.append(f"queue.translate.{k} must be one line using {' or '.join(TRANSLATE[k])}, or null")
    ad = q.get("adopt")
    if ad is not None and (not isinstance(ad, list) or not all(isinstance(x, str) and x.strip() for x in ad)):
        errs.append("queue.adopt must be a list of job-name globs, or null")
    if q.get("log_dir") is not None and not isinstance(q["log_dir"], str):
        errs.append("queue.log_dir must be a path on the remote host or null")
    if not isinstance(q.get("log_name"), str) or "{job}" not in q["log_name"]:
        errs.append("queue.log_name must contain {job}")
    if not isinstance(q.get("header_lines"), int) or q["header_lines"] < 1:
        errs.append("queue.header_lines must be an integer >= 1")
    return errs


def contract_ext(cfg: dict) -> dict:
    """The `contract:` section with `unknown: auto` resolved."""
    c = dict(cfg.get("contract") or {})
    if c.get("unknown", "auto") == "auto":
        c["unknown"] = "warn" if (cfg.get("queue") or {}).get("backend") == "ssh" else "error"
    return c


def load(p: Project) -> dict:
    if not p.config_path.exists():
        raise ConfigError(f"{p.config_path} missing: run `tw init`")
    raw = yaml.safe_load(p.config_path.read_text()) or {}
    cfg = _merge(DEFAULTS, raw)
    errs = validate(cfg)
    if errs:
        raise ConfigError("tierwright.yaml invalid:\n  " + "\n  ".join(errs))
    return cfg


def role(cfg: dict, name: str) -> dict:
    """Resolved settings for a role: tier, backend, model, effort."""
    base = dict(DEFAULT_ROLES.get(name, {"tier": "T2"}))
    base.update(cfg.get("roles", {}).get(name, {}) or {})
    tier = base["tier"]
    if cfg["backend"] == "both":
        backend = base.get("backend") or BOTH_SPLIT.get(name, "subscription")
    else:
        backend = cfg["backend"]
    out = {"role": name, "tier": tier, "backend": backend}
    if tier != "T0":
        tc = cfg["tiers"][tier]
        out["model"] = base.get("model", tc["model"])
        out["effort"] = base.get("effort", tc.get("effort", "medium"))
    return out


def render(backend: str, per_day: float, per_goal: float, mode: str = "full") -> str:
    """The tierwright.yaml written by `tw init`, commented so it is self-explaining."""
    split = "\n".join(f"#   {r}: {{backend: {b}}}" for r, b in BOTH_SPLIT.items())
    return f"""# Tierwright config. Docs: README.md (section Config).
version: 1

# full: jobs need output contracts (heavy CLI / infra work).
# lite: no job contracts; goals and steps use plain-language checks
#       (file:, words:, contains:, lacks:, review: ... against GUIDE).
mode: {mode}

# Where agents run: subscription (headless `claude -p` on your plan),
# api (Anthropic API key, pay per token), or both (split per role below).
backend: {backend}

budgets:            # USD. Over budget = the call waits and an owner-queue line says why.
  per_call_usd: 1.0
  per_day_usd: {per_day}
  per_goal_usd: {per_goal}

tiers:              # T0 = code, never a model.
  T1: {{model: claude-haiku-5-5, effort: low}}      # extraction, classification
  T2: {{model: claude-sonnet-5-5, effort: medium}}  # diagnosis, review
  T3: {{model: claude-opus-5-5, effort: high}}      # design, building

# Per-role overrides: tier, backend (only with backend: both), model, effort.
# Default split for backend: both:
{split}
roles: {{}}

limits: {{call_timeout_s: 900, escalate_after_failures: 2, job_timeout_s: 3600}}
dispatch: {{auto_diagnose: true}}   # call the diagnose role when no rule matches a failure
agents:
  push: never             # never | after_gate: may builder/task agents push once the gate passes
hooks:
  block_polling: true     # agents may not sleep/watch-loop on jobs
  block_raw_logs: true    # agents read `tw brief JOB`, not .tierwright/logs
  protected_paths: []     # globs agents may never edit (held-out sets, graders)
"""


def write_default(path: Path, backend: str, per_day: float = 10.0, per_goal: float = 50.0,
                  mode: str = "full") -> None:
    path.write_text(render(backend, per_day, per_goal, mode))
