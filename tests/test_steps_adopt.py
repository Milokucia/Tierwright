import pytest

from tierwright import adopt, ledger, steps


def test_t0_plan_starts_at_the_goal(proj, cfg):
    r = adopt.plan(proj, cfg, use_model=False)
    st = steps.load(proj)
    assert st[0]["id"] == "goal" and st[0]["status"] == "open"
    assert all(s["status"] == "locked" for s in st[1:])
    assert (proj.root / "GAP.md").exists() and r["source"] == "T0"


def test_gate_unlocks_next_and_records(proj, cfg):
    adopt.plan(proj, cfg, use_model=False)
    with pytest.raises(steps.StepError, match="locked"):
        steps.check(proj, "tests")
    r = steps.check(proj)
    assert not r["pass"]                               # hello.sh does not exist yet
    (proj.root / "hello.sh").write_text("echo hi\n")
    r = steps.check(proj)
    assert r["pass"] and r["next"] == "tests"
    assert [e["verdict"] for e in ledger.entries(proj, "step", "goal")] == ["fail", "win"]
    assert steps.done(proj)["pass"]


PLAN_BAD = {"summary": "s", "steps": [
    {"id": "a", "goal": "edit missing", "why": "w", "tier": "T2", "budget_usd": 1, "depends_on": ["z"],
     "changes": [{"path": "nope.py", "action": "edit", "what": "x"}], "gate": ["no-such-binary-xyz"]}]}
PLAN_GOOD = {"summary": "s", "steps": [
    {"id": "script", "goal": "add hello", "why": "w", "tier": "T1", "budget_usd": 0.2, "depends_on": [],
     "changes": [{"path": "hello.sh", "action": "add", "what": "echo hi"}],
     "gate": ["bash hello.sh | grep -q hi"]},
    {"id": "check", "goal": "run it", "why": "w", "tier": "T1", "budget_usd": 0.2, "depends_on": ["script"],
     "changes": [], "gate": ["./hello.sh"]}]}


def test_validate_plan_errors(proj, cfg):
    errs = adopt.validate_plan(proj, cfg, PLAN_BAD)
    text = "\n".join(errs)
    assert "file does not exist" in text and "not on PATH" in text and "not an earlier step" in text
    assert adopt.validate_plan(proj, cfg, PLAN_GOOD) == []   # ./hello.sh is added by an earlier step


def test_planner_rejected_then_valid(proj, cfg, fake):
    ex = {"purpose": "p", "public_api": [], "depends_on": [], "tested_by": []}
    fake({"planner": [PLAN_BAD, PLAN_GOOD], "extract": ex})
    r = adopt.plan(proj, cfg, use_model=True)
    assert r["source"] == "planner"
    assert [s["id"] for s in steps.load(proj)] == ["script", "check"]


def test_protected_paths_and_dead_ends(proj, cfg):
    cfg["hooks"]["protected_paths"] = ["heldout/*"]
    plan = {"summary": "", "steps": [{**PLAN_GOOD["steps"][0], "changes": [
        {"path": "heldout/x.json", "action": "add", "what": "x"}]}]}
    assert any("protected" in e for e in adopt.validate_plan(proj, cfg, plan))
    ledger.record(proj, "proposal", "p", "fail", change="add hello")
    assert any("repeats a failed" in e for e in adopt.validate_plan(proj, cfg, PLAN_GOOD))


def test_refresh_keeps_passed_steps(proj, cfg):
    adopt.plan(proj, cfg, use_model=False)
    (proj.root / "hello.sh").write_text("echo hi\n")
    steps.check(proj)
    adopt.plan(proj, cfg, use_model=False, refresh=True)
    st = steps.load(proj)
    assert st[0]["id"] == "goal" and st[0]["status"] == "passed"
    assert sum(1 for s in st if s["id"] == "goal") == 1


def test_inventory_ignores_generated(proj):
    inv = adopt.inventory(proj.root)
    assert inv["files"] >= 1
    assert not any(f.startswith(".claude/agents/tw-") for f in inv["agent_files"])


def test_repo_map_is_cached(proj, cfg, fake):
    (proj.root / "a.py").write_text("def f():\n    return 1\n")
    fake({"extract": {"purpose": "p", "public_api": ["f"], "depends_on": [], "tested_by": []}})
    assert adopt.repo_map(proj, cfg, use_model=True)["summarized_now"] == 1
    assert adopt.repo_map(proj, cfg, use_model=True)["summarized_now"] == 0
    (proj.root / "a.py").write_text("def g():\n    return 2\n")
    assert adopt.repo_map(proj, cfg, use_model=True)["summarized_now"] == 1
