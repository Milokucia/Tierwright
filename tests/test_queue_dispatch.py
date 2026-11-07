import pytest

from conftest import job
from tierwright import dispatch, jobqueue, ledger, router
from tierwright.jobqueue import QueueError


def run_all(p, cfg):
    out = []
    jobqueue.run(p, once=True, on_end=lambda m: out.extend(dispatch.poll(p, cfg)))
    return out


def test_job_without_contract_is_refused(proj):
    with pytest.raises(QueueError, match="no output contract"):
        jobqueue.submit(proj, job(proj, "bare", "echo hi\n"))


def test_ok_job_needs_no_model(proj, cfg):
    jobqueue.submit(proj, job(proj, "build", "#@ artifact hello.sh\necho 'echo hi' > hello.sh\n"))
    out = run_all(proj, cfg)
    assert [d["action"] for d in out] == ["ok"]
    assert router.runs(proj) == []          # zero model calls
    assert ledger.entries(proj, "job", "build")[-1]["verdict"] == "ok"


def test_contract_stops_garbage_job_and_diagnose_proposes(proj, cfg, fake):
    fake({"diagnose": {"hypotheses": [{"cause": "bad input", "evidence": "Traceback", "confidence": "high"}],
                       "proposal": {"action": "owner", "target": "train", "why": "code bug",
                                    "change": "fix parser"}}})
    jobqueue.submit(proj, job(proj, "train",
                              "#@ forbid tb re=Traceback\necho 'Traceback (most recent call last)'\nsleep 30\n"))
    out = run_all(proj, cfg)
    assert out[0]["action"] == "diagnose" and out[0]["rule"] == "contract-stop"
    assert out[0]["applied"] == "owner"
    assert "fix parser" in proj.owner_queue.read_text()
    assert (proj.attention / "train.json").exists()


def test_transient_is_resubmitted_once_then_diagnosed(proj, cfg):
    cfg["dispatch"]["auto_diagnose"] = False
    jobqueue.submit(proj, job(proj, "fetch",
                              "#@ forbid x re=NEVER\necho 'curl: (6) Could not resolve host: mirror'\nexit 6\n"))
    out = run_all(proj, cfg)
    assert [(d["job"], d["action"]) for d in out] == [("fetch", "resubmit"), ("fetch_r1", "diagnose")]
    assert out[1]["applied"] == "attention"


def test_dependency_waits_and_fails_with_upstream(proj, cfg):
    cfg["dispatch"]["auto_diagnose"] = False
    jobqueue.submit(proj, job(proj, "b", "#@ after a\n#@ forbid x re=NEVER\necho b\n"))
    jobqueue.submit(proj, job(proj, "a", "#@ forbid x re=NEVER\nexit 3\n"))
    out = run_all(proj, cfg)
    acts = {d["job"]: d["action"] for d in out}
    assert acts["a"] == "diagnose" and acts["b"] == "ignore"


def test_dead_end_proposal_is_blocked(proj, cfg, fake):
    ledger.record(proj, "proposal", "j", "fail", note="did not help", change="resubmit j: rerun", parent="j")
    fake({"diagnose": {"hypotheses": [], "proposal": {"action": "resubmit", "target": "j",
                                                       "why": "flaky", "change": "rerun"}}})
    jobqueue.submit(proj, job(proj, "j", "#@ forbid x re=NEVER\nexit 1\n"))
    out = run_all(proj, cfg)
    assert out[0]["applied"] == "blocked (dead end)"
