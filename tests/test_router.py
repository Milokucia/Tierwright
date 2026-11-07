import json

import pytest

from tierwright import config, ledger, router
from tierwright.project import write_json

GOOD = {"class": "network", "action": "resubmit", "note": "dns"}


def test_role_resolution_per_backend(cfg):
    assert config.role(cfg, "planner")["model"] == "claude-opus-5-5"
    assert config.role(cfg, "triage")["model"] == "claude-haiku-5-5"
    both = {**cfg, "backend": "both"}
    assert config.role(both, "triage")["backend"] == "api"
    assert config.role(both, "builder")["backend"] == "subscription"
    both["roles"] = {"triage": {"backend": "subscription"}}
    assert config.role(both, "triage")["backend"] == "subscription"


def test_config_validation_rejects_bad_backend_override():
    cfg = config._merge(config.DEFAULTS, {"backend": "api", "roles": {"x": {"backend": "subscription"}}})
    assert any("roles.x.backend" in e for e in config.validate(cfg))


def test_schema_reject_retry_then_escalate(proj, cfg, fake):
    fake({"triage": [{"class": "x"}, {"oops": 1}, GOOD]})
    out = router.call(proj, cfg, "triage", {"brief": {}})
    assert out == GOOD
    rs = sorted(router.runs(proj), key=lambda r: r["ts"])
    assert [r["tier"] for r in rs] == ["T1", "T1", "T2"]
    assert [r["outcome"] for r in rs] == ["rejected", "rejected", "ok"]
    esc = ledger.entries(proj, "escalation")
    assert esc and "T1 -> T2" in esc[0]["note"]


def test_caller_validation_counts_as_rejection(proj, cfg, fake):
    fake({"triage": [GOOD, {**GOOD, "note": "fine"}]})
    out = router.call(proj, cfg, "triage", {}, validate=lambda o: [] if o["note"] == "fine" else ["note"])
    assert out["note"] == "fine"


def test_top_tier_failure_raises(proj, cfg, fake):
    fake({"planner": {"bad": True}})
    with pytest.raises(router.CallFailed):
        router.call(proj, cfg, "planner", {})


def _spent(p, backend, usd):
    write_json(p.runs / f"9999999999-spent-{backend}.json",
               {"id": "x", "ts": router._day_start() + 10, "role": "triage", "tier": "T1",
                "backend": backend, "cost_usd": usd, "outcome": "ok", "usage": {}})


def test_budget_blocks_and_notes_owner(proj, cfg, fake):
    fake({"triage": GOOD})
    _spent(proj, "subscription", 100)
    with pytest.raises(router.BudgetExceeded):
        router.call(proj, cfg, "triage", {})
    assert "Budget reached" in proj.owner_queue.read_text()


def test_both_moves_to_other_backend_when_one_is_out(proj, cfg, fake):
    fake({"triage": GOOD})
    cfg = {**cfg, "backend": "both"}
    _spent(proj, "api", 100)
    router.call(proj, cfg, "triage", {})
    assert any("moved to subscription" in e["note"] for e in ledger.entries(proj, "escalation"))
