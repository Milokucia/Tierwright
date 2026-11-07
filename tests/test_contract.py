import json
import time

import pytest

from tierwright import contract
from tierwright.contract import ContractError, Verifier, parse


def test_parse_rejects_unknown_kind_and_option():
    with pytest.raises(ContractError, match="unknown check"):
        parse("#@ nope x")
    with pytest.raises(ContractError, match="takes no option"):
        parse("#@ forbid crash re=x bogus=1")
    with pytest.raises(ContractError, match="needs re="):
        parse("#@ forbid crash")
    with pytest.raises(ContractError, match="bad regex"):
        parse("#@ forbid crash re='('")


def test_has_contract():
    assert not contract.has_contract("echo hi\n#@ after other\n")
    assert contract.has_contract("#@ forbid tb re=Traceback\n")


def _v(tmp_path, text, log_text="", start=None):
    log = tmp_path / "out.log"
    log.write_text(log_text)
    return Verifier(text, str(log), start or time.time() - 1, cwd=str(tmp_path)), log


def test_forbid_and_value(tmp_path):
    v, _ = _v(tmp_path, "#@ forbid tb re=Traceback\n#@ value loss re='loss (\\S+)' finite max=10",
              "loss 3.0\nloss nan\n")
    res = {r["check"]: r for r in v.check(time.time())}
    assert res["tb"]["status"] == "ok"
    assert res["loss"]["status"] == "fail"


def test_end_only_checks(tmp_path):
    start = time.time() - 1
    v, _ = _v(tmp_path, "#@ artifact out.json\n#@ result out.json keys=a,b.c\n"
                        "#@ success rate re='passed (\\d+)/(\\d+)' min=0.8", "passed 9/10\n", start)
    assert all(r["status"] == "pending" for r in v.check(time.time()))
    (tmp_path / "out.json").write_text(json.dumps({"a": 1, "b": {"c": 2}}))
    res = v.check(time.time(), ended=True)
    assert contract.verdict(res)["pass"], res


def test_result_missing_key_and_stale_artifact(tmp_path):
    (tmp_path / "old.json").write_text("{}")
    v, _ = _v(tmp_path, "#@ artifact old.json\n#@ result old.json keys=x", "", time.time() + 5)
    res = {r["kind"]: r for r in v.check(time.time(), ended=True)}
    assert res["artifact"]["status"] == "fail"  # a previous run's file does not count


def test_progress_stall(tmp_path):
    v, log = _v(tmp_path, "#@ progress steps re='step (\\d+)' stall=2s", "step 1\n")
    t = time.time()
    assert v.check(t)[0]["status"] == "ok"
    assert v.check(t + 1)[0]["status"] == "ok"
    assert v.check(t + 3)[0]["status"] == "fail"


def test_timeout_and_phase(tmp_path):
    start = time.time() - 100
    v, _ = _v(tmp_path, "#@ timeout 30s\n#@ phase ready until=READY max=50s", "", start)
    res = {r["kind"]: r["status"] for r in v.check(time.time())}
    assert res == {"timeout": "fail", "phase": "fail"}


def test_warn_mode_is_not_hard(tmp_path):
    v, _ = _v(tmp_path, "#@ mode warn\n#@ forbid tb re=Traceback", "Traceback (most recent call last)")
    vd = contract.verdict(v.check(time.time()))
    assert not vd["pass"] and not vd["hard_fail"]
