"""`tierwright.yaml`: backend choice, budgets, tiers, role overrides, hooks.

Validated on every load; a bad config fails loudly, never half-applies.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from .project import Project, ProjectError

BACKENDS = ("subscription", "api", "both")
TIERS = ("T0", "T1", "T2", "T3")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Roles and their default tier. T0 roles never call a model.
DEFAULT_ROLES = {
    "triage":   {"tier": "T1"},   # classify a failure no rule matched
    "extract":  {"tier": "T1"},   # repo-map summaries, field extraction
    "reporter": {"tier": "T1"},   # one-line prose for status / handoff
    "diagnose": {"tier": "T2"},   # ranked hypotheses + one probe
    "review":   {"tier": "T2"},   # milestone / code-area review
    "planner":  {"tier": "T3"},   # goal + codebase -> step ladder
    "builder":  {"tier": "T3"},   # build a step in a worktree
}

# With backend "both": which backend each role uses unless overridden.
# High-volume / batchable roles go to the API; long interactive builds to the plan.
BOTH_SPLIT = {"triage": "api", "extract": "api", "reporter": "api", "review": "api",
              "diagnose": "subscription", "planner": "subscription", "builder": "subscription"}

DEFAULTS = {
    "version": 1,
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
    "hooks": {"block_polling": True, "block_raw_logs": True, "protected_paths": []},
}


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
    return errs


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


def render(backend: str, per_day: float, per_goal: float) -> str:
    """The tierwright.yaml written by `tw init`, commented so it is self-explaining."""
    split = "\n".join(f"#   {r}: {{backend: {b}}}" for r, b in BOTH_SPLIT.items())
    return f"""# Tierwright config. Docs: README.md (section Config).
version: 1

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
hooks:
  block_polling: true     # agents may not sleep/watch-loop on jobs
  block_raw_logs: true    # agents read `tw brief JOB`, not .tierwright/logs
  protected_paths: []     # globs agents may never edit (held-out sets, graders)
"""


def write_default(path: Path, backend: str, per_day: float = 10.0, per_goal: float = 50.0) -> None:
    path.write_text(render(backend, per_day, per_goal))
