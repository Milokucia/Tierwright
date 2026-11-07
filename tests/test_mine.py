import json

import pytest

from conftest import job
from tierwright import dispatch, jobqueue, ledger, mine, rules


def transcript(path, calls):
    """calls: [(tool, input, result_text)] -> a Claude Code style jsonl."""
    lines = []
    for i, (tool, inp, res) in enumerate(calls):
        tid = f"t{i}"
        lines.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": tool, "input": inp}]}})
        lines.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": res, "is_error": False}]}})
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return path


@pytest.fixture
def tx(proj, tmp_path):
    (proj.root / "big.md").write_text("x" * 9000)
    root = str(proj.root)
    calls = []
    for _ in range(4):
        calls += [("Bash", {"command": f"cd {root} && git status --short"}, "M a\n" * 50),
                  ("Bash", {"command": "ls"}, "a\nb\n" * 200),
                  ("Bash", {"command": "sleep 60 && tw status"}, "queue: 1 running"),
                  ("Read", {"file_path": f"{root}/big.md"}, "x" * 9000)]
    for _ in range(3):
        calls += [("Bash", {"command": "rm -rf build && make"}, "cc a.c\n" * 300),
                  ("Bash", {"command": "ls -la build"}, "a\n" * 300)]
    d = tmp_path / "tx"
    d.mkdir()
    transcript(d / "s1.jsonl", calls)
    return [str(d)]


def test_patterns_and_ranking(proj, tx):
    r = mine.run(proj, dirs=tx, min_count=3)
    kinds = {x["kind"] for x in r["proposals"]}
    assert {"tool", "digest", "note"} <= kinds
    seq = next(x for x in r["proposals"] if x["kind"] == "tool" and x["cmds"][0] == "git status --short")
    assert seq["cmds"] == ["git status --short", "ls"]     # cd <root> stripped, a 2-command sequence
    assert any("polled" in x["why"] for x in r["proposals"])
    assert (proj.root / "MINE.md").exists()
    ests = [x["est_tokens"] for x in r["proposals"]]
    assert ests == sorted(ests, reverse=True)


def test_read_only_gate():
    assert mine.read_only("git status --short && ls | head -5")
    assert not mine.read_only("rm -rf build && ls")
    assert not mine.read_only("git push origin main")
    assert not mine.read_only("echo x > f")
    assert not mine.read_only("tail -f log")
    assert not mine.read_only("python3 train.py")


def test_unsafe_tool_is_never_applied(proj, cfg, tx):
    r = mine.run(proj, dirs=tx, min_count=3)
    bad = next(x for x in r["proposals"] if x["kind"] == "tool" and "rm -rf" in x["cmds"][0])
    assert not bad["valid"]
    with pytest.raises(ValueError, match="not installed"):
        mine.apply(proj, cfg, bad["id"])


def test_apply_tool_runs_and_is_tracked(proj, cfg, tx, capfd):
    (proj.root / "a").write_text("1")
    r = mine.run(proj, dirs=tx, min_count=3)
    seq = next(x for x in r["proposals"] if x["kind"] == "tool" and x["cmds"][0] == "git status --short")
    assert seq["valid"], seq["errors"]
    msg = mine.apply(proj, cfg, seq["id"])
    name = msg.split()[1]
    assert mine.run_tool(proj, name) == 0
    out = capfd.readouterr().out
    assert "$ git status --short" in out and "$ ls" in out
    assert f"tw tool {name}" in (proj.root / "CLAUDE.md").read_text()
    assert ledger.entries(proj, "mine")[-1]["name"] == seq["id"]
    # usage is measured on the next mine
    transcript(__import__("pathlib").Path(tx[0]) / "s2.jsonl", [("Bash", {"command": f"tw tool {name}"}, "ok")])
    r2 = mine.run(proj, dirs=tx, min_count=3)
    assert r2["applied"][name]["uses"] == 1


def test_digest_is_cached_by_hash(proj, cfg, fake, tx):
    fake({"extract": {"purpose": "big notes", "public_api": [], "depends_on": [], "tested_by": []}})
    r = mine.run(proj, dirs=tx, min_count=3)
    dg = next(x for x in r["proposals"] if x["kind"] == "digest")
    mine.apply(proj, cfg, dg["id"])
    out = proj.root / ".tierwright" / "digests" / "big.md.md"
    assert "big notes" in out.read_text()
    n = len(list(proj.runs.glob("*.json")))
    mine.digest(proj, cfg, "big.md")
    assert len(list(proj.runs.glob("*.json"))) == n        # unchanged file: no model call
    (proj.root / "big.md").write_text("y" * 100)
    mine.digest(proj, cfg, "big.md")
    assert len(list(proj.runs.glob("*.json"))) == n + 1


def test_repeated_unmatched_failure_becomes_rule(proj, cfg, tmp_path):
    cfg["dispatch"]["auto_diagnose"] = False
    for i in (1, 2):
        jobqueue.submit(proj, job(proj, f"etl{i}",
                                  f"#@ forbid x re=NEVER\necho 'schema drift in table orders col {i}'\nexit 1\n"))
    jobqueue.run(proj, once=True, on_end=lambda m: dispatch.poll(proj, cfg))
    empty = tmp_path / "none"
    empty.mkdir()
    r = mine.run(proj, dirs=[str(empty)], min_count=3)
    rule = next(x for x in r["proposals"] if x["kind"] == "rule")
    assert rule["valid"], rule["errors"]
    mine.apply(proj, cfg, rule["id"])
    assert rules.load(proj)[0].name == f"mined-{rule['id']}"
