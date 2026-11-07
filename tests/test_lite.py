import pytest

from conftest import job
from tierwright import adopt, checks, config, init, jobqueue, steps
from tierwright.project import Project

POST = "Acme launches today. Book a demo at acme.com/demo. " * 20


@pytest.fixture
def lite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TIERWRIGHT_FAKE", raising=False)
    init.run(str(tmp_path), backend="api", goal="weekly newsletter",
             done=["file: out/post.md", "review: out/post.md against brand.md"],
             probe=False, interactive=False, lite=True)
    (tmp_path / "brand.md").write_text("Voice: plain, no hype. Always include the demo link.\n")
    return Project(tmp_path).ensure()


def test_lite_config_and_claude_section(lite):
    cfg = config.load(lite)
    assert cfg["mode"] == "lite"
    assert "Tierwright lite" in (lite.root / "CLAUDE.md").read_text()


@pytest.mark.parametrize("item,err", [
    ("words: a.md lots", "MIN-MAX"), ("contains: a.md", "PATH: term"),
    ("review: a.md", "against"), ({"file": "a", "words": "b"}, "one key"), ({"nope": 1}, "unknown"),
])
def test_bad_checks_are_rejected(item, err):
    with pytest.raises(checks.CheckError, match=err):
        checks.parse(item)


def test_plain_checks(lite):
    (lite.root / "out").mkdir()
    (lite.root / "out/post.md").write_text(POST)
    ok = lambda c: checks.run(lite, c)["rc"] == 0
    assert ok("file: out/post.md") and not ok("file: out/missing.md")
    assert ok("words: out/post.md 100-300") and not ok("words: out/post.md >=1000")
    assert ok("contains: out/post.md: acme, ACME.COM/DEMO")
    r = checks.run(lite, "contains: out/post.md: acme, pricing")
    assert r["rc"] == 1 and "pricing" in r["tail"][0]
    assert ok("lacks: out/post.md: lorem, TODO") and not ok("lacks: out/post.md: launches")
    assert ok({"run": "true"}) and ok("true") and not ok("false")


def test_review_check_uses_judge_and_needs_reasons(lite, fake):
    (lite.root / "out").mkdir()
    (lite.root / "out/post.md").write_text(POST)
    fake({"judge": {"verdict": "pass", "reasons": []}})
    assert checks.run(lite, "review: out/post.md against brand.md")["rc"] == 0
    # a fail without a reason is rejected, retried, then accepted with one
    fake({"judge": [{"verdict": "fail", "reasons": []},
                    {"verdict": "fail", "reasons": ["'no hype' broken: 'revolutionary'"]}]})
    r = checks.run(lite, "review: out/post.md against brand.md: no superlatives")
    assert r["rc"] == 1 and "no hype" in r["tail"][0]
    assert checks.run(lite, "review: out/post.md against missing.md")["rc"] == 1


def test_lite_goal_loop(lite, fake):
    cfg = config.load(lite)
    adopt.plan(lite, cfg, use_model=False)
    assert [s["id"] for s in steps.load(lite)] == ["goal"]          # no tests/CI hardening in lite
    assert not steps.check(lite)["pass"]
    (lite.root / "out").mkdir()
    (lite.root / "out/post.md").write_text(POST)
    fake({"judge": {"verdict": "pass", "reasons": []}})
    assert steps.check(lite)["pass"] and steps.done(lite)["pass"]


def test_plan_validator_knows_checks(lite):
    cfg = config.load(lite)
    st = {"id": "a", "goal": "g", "why": "w", "tier": "T1", "budget_usd": 1, "depends_on": [],
          "changes": [{"path": "out/x.md", "action": "add", "what": "draft"}]}
    good = {"summary": "", "steps": [{**st, "gate": ["file: out/x.md", "words: out/x.md 100-200",
                                                     "review: out/x.md against brand.md"]}]}
    assert adopt.validate_plan(lite, cfg, good) == []
    bad = {"summary": "", "steps": [{**st, "gate": ["review: out/x.md against nope.md", "words: x 5"]}]}
    errs = "\n".join(adopt.validate_plan(lite, cfg, bad))
    assert "nope.md does not exist" in errs and "MIN-MAX" in errs


def test_lite_jobs_need_no_contract(lite):
    jobqueue.submit(lite, job(lite, "draft", "echo hi\n"), require_contract=False)
    assert (lite.q("pending") / "draft.job").exists()


def test_full_mode_still_requires_contract(proj):
    with pytest.raises(jobqueue.QueueError):
        jobqueue.submit(proj, job(proj, "bare", "echo hi\n"))
