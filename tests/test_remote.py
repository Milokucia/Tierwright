"""The ssh queue backend against a fake box: a temp dir as the remote HOME,
`ssh HOST CMD` run by bash there, `scp` a copy. No network."""

import shutil
import subprocess

import pytest
import yaml

from conftest import job
from tierwright import brief, cli, config, dispatch, remote, router
from tierwright.config import ConfigError
from tierwright.contract import ContractError, parse
from tierwright.project import read_jsonl

GOOD = "#@ success smi re='GPUS (\\d+)/(\\d+)' min=1 pct hard\n#@ tag gq-x\nnvidia-smi -L\n"


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "box"
    for d in ("gq/pending", "gq/running", "gq/done"):
        (home / d).mkdir(parents=True)
    (home / "fill_ledger.txt").write_text("2026-11-10 10:00:00 GQ start old.job on GPU 0\n")

    def fake(argv, capture_output=True, text=True, timeout=None, cwd=None):
        if argv[0] == "ssh":
            if "unreachable" in argv[-2]:
                err = "ssh: connect to host unreachable port 22: Connection timed out\n"
                return subprocess.CompletedProcess(argv, 255, "" if text else b"",
                                                   err if text else err.encode())
            return subprocess.run(["bash", "-c", argv[-1]], capture_output=True, text=text,
                                  env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
        if argv[0] == "scp":
            dst = argv[-1].split(":", 1)[1]
            shutil.copy(argv[-2], home / dst)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(argv, capture_output=True, text=text, cwd=cwd)

    monkeypatch.setattr(remote, "RUNNER", fake)
    return home


def use_ssh(proj, **over):
    raw = yaml.safe_load(proj.config_path.read_text())
    raw["queue"] = {"backend": "ssh", "host": "ubuntu@fakebox", **over}
    raw["dispatch"] = {"auto_diagnose": False}
    proj.config_path.write_text(yaml.safe_dump(raw))
    return config.load(proj)


def ledger(box, *lines):
    with open(box / "fill_ledger.txt", "a") as f:
        for l in lines:
            f.write(l + "\n")


def pull(proj, cfg):
    out = []
    remote.run(proj, cfg, once=True, on_end=lambda m: out.extend(dispatch.poll(proj, cfg)))
    return out


def test_submit_copies_job_to_box_and_tolerates_box_flags(proj, box):
    cfg = use_ssh(proj)
    name, warn = remote.Remote(proj, cfg).submit(job(proj, "smi", GOOD))
    assert name == "smi" and (box / "gq/pending/smi.job").read_text() == GOOD
    assert any("'pct'" in w for w in warn) and any("'tag'" in w for w in warn)
    assert int((proj.state / "queue/remote.offset").read_text()) == (box / "fill_ledger.txt").stat().st_size
    with pytest.raises(remote.RemoteError, match="already queued"):
        remote.Remote(proj, cfg).submit(job(proj, "smi", GOOD))


def test_box_dialect_from_config_silences_warnings(proj, box):
    raw = yaml.safe_load(proj.config_path.read_text())
    raw["contract"] = {"kinds": ["tag"], "flags": ["pct", "hard"]}
    proj.config_path.write_text(yaml.safe_dump(raw))
    cfg = use_ssh(proj)
    _, warn = remote.Remote(proj, cfg).submit(job(proj, "smi", GOOD))
    assert warn == []


def test_local_lint_stays_strict_without_ssh(proj, cfg):
    with pytest.raises(ContractError, match="unknown flag|takes no option|unknown check"):
        parse(GOOD, config.contract_ext(cfg))
    assert parse("#@ success s re='(\\d+)/(\\d+)' min=1 pct\n", {"flags": ["pct"]})


def test_submit_refuses_name_already_done_on_box(proj, box):
    cfg = use_ssh(proj)
    (box / "gq/done/smi.job").write_text("old")
    with pytest.raises(remote.RemoteError, match="already pending, running or done"):
        remote.Remote(proj, cfg).submit(job(proj, "smi", GOOD))


def test_queue_run_never_runs_locally_and_reads_pass(proj, box):
    cfg = use_ssh(proj)
    body = "#@ forbid x re=NEVER\ntouch ran_here\n"
    remote.Remote(proj, cfg).submit(job(proj, "smi", body))
    assert pull(proj, cfg) == []
    assert not (proj.root / "ran_here").exists()
    ledger(box, "2026-11-10 10:01:00 GQ start smi.job on GPU 2",
           "2026-11-10 10:01:01 GQ start smi2.job on GPU 1",
           "2026-11-10 10:01:30 SMI GPUS 4/4 [gq-smi]",
           "2026-11-10 10:02:00 GQ end smi.job rc=0 after 0.0 h",
           "2026-11-10 10:02:01 GQ CONTRACT PASS smi.job rc=0: 1 checks ok [gq-smi]")
    out = pull(proj, cfg)
    assert [(d["job"], d["action"]) for d in out] == [("smi", "ok")]
    assert router.runs(proj) == []                       # no model call
    kinds = [e["kind"] for e in read_jsonl(proj.events)]
    assert "start" in kinds and "end" in kinds
    b = brief.build(proj, "smi")
    assert b["rc"] == 0 and b["contract_pass"] and b["duration_s"] == 61
    assert any("GPUS 4/4" in l for l in b["tail"]) and not any("smi2" in l for l in b["tail"])
    assert pull(proj, cfg) == []                         # offset: nothing read twice
    assert not (proj.root / "ran_here").exists()


def test_health_stop_and_fail_reach_diagnose(proj, box):
    cfg = use_ssh(proj)
    remote.Remote(proj, cfg).submit(job(proj, "train", GOOD))
    ledger(box, "2026-11-10 10:01:00 GQ start train.job on GPU 0",
           "2026-11-10 10:05:00 GQ HEALTH stop train.job: contract forbid broken: 1x: NaN",
           "2026-11-10 10:05:30 GQ CONTRACT FAIL train.job rc=-15: forbid broken: 1x: NaN; "
           "progress steps: stalled 10m")
    out = pull(proj, cfg)
    assert out[0]["action"] == "diagnose" and out[0]["rule"] == "contract-stop"
    b = brief.build(proj, "train")
    assert b["reason"] == "contract_stop" and b["rc"] == -15
    assert b["contract_failed"] == ["forbid broken: 1x: NaN", "progress steps: stalled 10m"]
    assert (proj.attention / "train.json").exists()


def test_resubmit_goes_to_the_box(proj, box):
    cfg = use_ssh(proj)
    remote.Remote(proj, cfg).submit(job(proj, "fetch", GOOD))
    ledger(box, "2026-11-10 10:01:00 GQ start fetch.job on GPU 0 [gq-fetch]",
           "2026-11-10 10:01:05 curl: (6) Could not resolve host: mirror [gq-fetch]",
           "2026-11-10 10:01:06 GQ CONTRACT FAIL fetch.job rc=6: success smi: no match")
    out = pull(proj, cfg)
    assert out[0]["action"] == "resubmit"
    pull(proj, cfg)
    assert (box / "gq/pending/fetch_r1.job").exists()
    assert not list(proj.q("pending").glob("*.job"))


def test_submit_cmd_gets_file_name_and_extra_args(proj, box, tmp_path):
    seen = tmp_path / "seen.txt"
    cfg = use_ssh(proj, submit_cmd=f"echo {{file}} {{name}} {{host}} > {seen}; echo")
    remote.Remote(proj, cfg).submit(job(proj, "smi", GOOD), "smi_b", ["--tree", "abc"])
    s = seen.read_text().split()
    assert s[1:] == ["smi_b", "ubuntu@fakebox"] and s[0].endswith("smi.job")
    assert not (box / "gq/pending/smi_b.job").exists()   # the script, not scp, copies
    bad = use_ssh(proj, submit_cmd="echo 'no ~/deploy/abc on box' >&2; exit 1")
    with pytest.raises(remote.RemoteError, match="rc=1.*no ~/deploy/abc"):
        remote.Remote(proj, bad).submit(job(proj, "smi2", GOOD))


def test_unreachable_host_is_reported_cleanly(proj, box, capsys):
    use_ssh(proj, host="ubuntu@unreachable")
    rc = cli.main(["submit", job(proj, "smi", GOOD)])
    err = capsys.readouterr().err
    assert rc == 1 and err.startswith("tw: cannot reach ubuntu@unreachable") and "Traceback" not in err
    assert cli.main(["queue", "run"]) == 1
    assert "cannot reach" in capsys.readouterr().err


@pytest.mark.parametrize("queue,msg", [
    ({"backend": "slurm"}, "queue.backend must be one of"),
    ({"backend": "ssh"}, "queue.host must be"),
    ({"backend": "ssh", "host": "h", "key": "/no/such.pem"}, "queue.key /no/such.pem does not exist"),
    ({"backend": "ssh", "host": "h", "events": {"pass": "PASS (\\S+)"}}, "needs a (?P<job>...) group"),
    ({"backend": "ssh", "host": "h", "events": {"fail": "("}}, "queue.events.fail: bad regex"),
    ({"backend": "ssh", "host": "h", "events": {"done": "x"}}, "unknown event"),
    ({"backend": "ssh", "host": "h", "poll_s": 0}, "queue.poll_s must be"),
])
def test_bad_queue_config_is_rejected(proj, queue, msg):
    raw = yaml.safe_load(proj.config_path.read_text())
    raw["queue"] = queue
    proj.config_path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match=msg.replace("(", "\\(").replace(")", "\\)").replace("?", "\\?")):
        config.load(proj)


def test_stop_removes_a_pending_box_job(proj, box):
    cfg = use_ssh(proj)
    r = remote.Remote(proj, cfg)
    r.submit(job(proj, "smi", GOOD))
    assert r.stop("smi") and not (box / "gq/pending/smi.job").exists()
    assert not r.stop("smi")
    assert dispatch.poll(proj, cfg)[0]["action"] == "ignore"


def test_cli_passes_extra_args_after_double_dash(proj, box, tmp_path):
    seen = tmp_path / "seen.txt"
    use_ssh(proj, submit_cmd=f"printf '%s ' {{name}} >{seen}; printf '%s ' >>{seen}")
    assert cli.main(["submit", job(proj, "smi", GOOD), "--name", "smi_c", "--", "--tree", "abc"]) == 0
    assert seen.read_text().split() == ["smi_c", "--tree", "abc"]
