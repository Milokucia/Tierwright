"""No watchers: trends of running jobs, `tw watch`, the `tune` role, agent
usage in `tw spend`, and the builder per step tier. Fake box, fake backend."""

import json
import shutil
import subprocess

import pytest
import yaml

from conftest import job
from tierwright import brief, cli, config, dispatch, hooks, ledger, remote, steps, trend, tune, usage, watch

JOB = ("#@ success ev in=ledger re='eval (\\d+)%' pct min=0.9\n"
       "#@ progress steps re='step (\\d+)' stall=10m\n"
       "#@ value loss re='loss (\\S+)' finite\n"
       "PATIENCE=3 REC_ENVS=1024 bash train.sh --lr 0.001\n")

KNOBS = {"PATIENCE": {"type": "int", "min": 1, "max": 6},
         "REC_ENVS": {"type": "int", "min": 256, "max": 4096, "scales": True},
         "DART_NOISE": {"type": "choice", "choices": ["0.2 0.4", "0.1 0.2"], "default": "0.2 0.4"},
         "lr": {"flag": "--lr", "type": "float", "min": 1e-5, "max": 1e-2},
         "SPEC": {"type": "str", "default": "specs/a.json"}}


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "box"
    for d in ("gq/pending", "gq/running", "gq/done", "gq/logs"):
        (home / d).mkdir(parents=True)
    (home / "fill_ledger.txt").write_text("")

    def fake(argv, capture_output=True, text=True, timeout=None, cwd=None):
        if argv[0] == "ssh":
            return subprocess.run(["bash", "-c", argv[-1]], capture_output=True, text=text,
                                  env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
        if argv[0] == "scp":
            shutil.copy(argv[-2], home / argv[-1].split(":", 1)[1])
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(argv, capture_output=True, text=text, cwd=cwd)

    monkeypatch.setattr(remote, "RUNNER", fake)
    return home


def setup(proj, tune_cfg=None, **raw_over):
    raw = yaml.safe_load(proj.config_path.read_text())
    raw["queue"] = {"backend": "ssh", "host": "ubuntu@fakebox"}
    raw["dispatch"] = {"auto_diagnose": False}
    raw["hooks"] = {"protected_paths": ["specs/heldout*"]}
    if tune_cfg is not None:
        raw["tune"] = tune_cfg
    raw.update(raw_over)
    proj.config_path.write_text(yaml.safe_dump(raw))
    return config.load(proj)


def add(box, path, *lines):
    with open(box / path, "a") as f:
        for l in lines:
            f.write(l + "\n")


def tick(proj, cfg):
    got = {"dispatch": []}
    remote.run(proj, cfg, once=True, on_end=lambda m: got["dispatch"].extend(dispatch.poll(proj, cfg)),
               on_tick=lambda t, w, u: got.update(trends=t, fired=w, tuned=u))
    return got


def start(proj, cfg, box, name="imit", body=JOB):
    remote.Remote(proj, cfg).submit(job(proj, name, body))
    add(box, "fill_ledger.txt", f"2026-11-10 10:00:00 GQ start {name}.job on GPU 0")


# ── trends ──────────────────────────────────────────────────────────────────
def test_queue_run_reads_running_job_trend_once_per_byte(proj, box):
    cfg = setup(proj)
    start(proj, cfg, box)
    add(box, "gq/logs/imit.job.log", "step 1 loss 0.9", "step 2 loss 0.7", "step 3 loss 0.5")
    add(box, "fill_ledger.txt", "2026-11-10 10:10:00 EVAL imit eval 40%",
        "2026-11-10 10:20:00 EVAL imit eval 55%")
    got = tick(proj, cfg)
    tr = got["trends"]["imit"]
    assert tr["state"] == "running" and tr["success"] == 0.55
    assert tr["series"]["loss"]["last"] == 0.5 and tr["series"]["loss"]["n"] == 3
    assert tr["series"]["ev"]["slope_per_h"] == pytest.approx(0.9)       # +0.15 in 10 min
    assert tr["progress"]["steps"]["last"] == "3"
    add(box, "gq/logs/imit.job.log", "step 4 loss 0.4", "partial line without newline")
    with open(box / "gq/logs/imit.job.log", "a") as f:
        f.write("step 5 loss 0.3")                                    # no newline yet: read later
    tick(proj, cfg)
    tr = trend.compact(proj, "imit")
    assert tr["series"]["loss"]["n"] == 4 and tr["series"]["loss"]["last"] == 0.4
    b = brief.build(proj, "imit")                                     # running: trend only
    assert b["state"] == "running" and b["trend"]["success"] == 0.55


def test_stall_shows_in_the_trend(proj, box):
    cfg = setup(proj)
    start(proj, cfg, box)
    add(box, "gq/logs/imit.job.log", "2026-11-10 10:01:00 step 7 loss 0.5")
    tr = tick(proj, cfg)["trends"]["imit"]
    assert tr["stalled"] and tr["progress"]["steps"]["stalled"]


def test_vars_fill_regexes(proj, box):
    cfg = setup(proj)
    body = ("#@ var TAG in=ledger re='spread (\\S+) \\('\n"
            "#@ success held in=ledger re='held ${TAG}: ([0-9.]+)' min=0.5\n#@ tag gq-x\ntrue\n")
    start(proj, cfg, box, "sp", body)
    add(box, "fill_ledger.txt", "2026-11-10 10:01:00 SS sp spread run7 (x)",
        "2026-11-10 10:02:00 HO sp held other: 0.1", "2026-11-10 10:03:00 HO sp held run7: 0.8")
    assert tick(proj, cfg)["trends"]["sp"]["success"] == 0.8


# ── tw watch ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["", "success>>0.9", "success>=high", "finished", "loss.median<1"])
def test_watch_rejects_bad_conditions(bad):
    with pytest.raises(watch.WatchError):
        watch.parse(bad)


def test_watch_needs_a_known_job(proj, box):
    cfg = setup(proj)
    with pytest.raises(watch.WatchError, match="no job"):
        watch.add(proj, cfg, "nope", "ended")


def test_watch_fires_once_with_attention_and_pushover(proj, box, monkeypatch, capsys):
    cfg = setup(proj)
    start(proj, cfg, box)
    sent = []
    monkeypatch.setenv("PUSHOVER_TOKEN", "t")
    monkeypatch.setenv("PUSHOVER_USER", "u")
    monkeypatch.setattr(watch, "SENDER", lambda url, data: sent.append(data))
    assert cli.main(["watch", "imit", "--until", "success>=0.9|ended"]) == 0
    assert "End your turn" in capsys.readouterr().out
    add(box, "fill_ledger.txt", "2026-11-10 10:10:00 EVAL imit eval 50%")
    assert tick(proj, cfg)["fired"] == []
    add(box, "fill_ledger.txt", "2026-11-10 10:20:00 EVAL imit eval 92%")
    fired = tick(proj, cfg)["fired"]
    assert len(fired) == 1 and fired[0]["fired"].startswith("success>=0.9") and fired[0]["pushed"]
    att = json.loads((proj.attention / "watch-imit.json").read_text())
    assert att["trend"]["success"] == 0.92 and b"success" in sent[0]
    assert watch.load(proj) == {} and tick(proj, cfg)["fired"] == []


def test_watch_on_an_ended_job_fires_not_met(proj, box, monkeypatch):
    cfg = setup(proj)
    monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
    monkeypatch.setattr(watch, "_pushover_creds", lambda cfg: {})
    start(proj, cfg, box)
    watch.add(proj, cfg, "imit", "success>=0.9")
    add(box, "fill_ledger.txt", "2026-11-10 10:10:00 EVAL imit eval 50%",
        "2026-11-10 11:00:00 GQ CONTRACT FAIL imit.job rc=0: success ev: 0.5 < 0.9")
    fired = tick(proj, cfg)["fired"]
    assert fired[0]["fired"] == "ended; success>=0.9 not met" and not fired[0]["pushed"]


def test_polling_hook_points_to_tw_watch(tmp_path):
    ev = lambda c: {"tool_name": "Bash", "tool_input": {"command": c}, "cwd": str(tmp_path)}
    for c in ("sleep 60", "watch -n 5 tw status", "ls && watch tail x", "while true; do tw status; sleep 5; done"):
        msg = hooks.decide(ev(c))
        assert msg and "use tw watch" in msg and "--until" in msg, c
    assert hooks.decide(ev("tw watch imit --until 'success>=0.9|ended'")) is None
    assert hooks.decide(ev("python3 x.py  # watch notifications")) is None


# ── tune ────────────────────────────────────────────────────────────────────
JC = {"match": "imit*", "metric": "ev", "target": 0.9, "max_scale": 2.0, "knobs": KNOBS}
CUR = {"PATIENCE": "3", "REC_ENVS": "1024", "DART_NOISE": "0.2 0.4", "lr": "0.001", "SPEC": "specs/a.json"}


def ans(*changes):
    return {"action": "change", "changes": [{"knob": k, "value": v} for k, v in changes], "why": "flat"}


@pytest.mark.parametrize("a,msg", [
    (ans(("EPOCHS", "40")), "not on this job's whitelist"),
    (ans(("PATIENCE", "9")), "out of range"),
    (ans(("PATIENCE", "two")), "is not an int"),
    (ans(("DART_NOISE", "0.5 0.9")), "not one of"),
    (ans(("PATIENCE", "3")), "current value"),
    (ans(("PATIENCE", "5")), "already tried"),
    (ans(("REC_ENVS", "4096")), "over budget"),
    (ans(("SPEC", "specs/heldout_v2.json")), "protected path"),
    (ans(("PATIENCE", "4"), ("PATIENCE", "5")), "listed twice"),
    (ans(), "needs at least one change"),
    ({"action": "none", "changes": [{"knob": "PATIENCE", "value": "4"}], "why": ""}, "must not list"),
])
def test_tune_check_rejects_bad_answers(a, msg):
    errs = tune.check(a, JC, CUR, {"patience=5"}, ["specs/heldout*"])
    assert any(msg in e for e in errs), errs


def test_tune_check_accepts_a_good_answer():
    assert tune.check(ans(("PATIENCE", "5"), ("REC_ENVS", "2048"), ("lr", "0.0005")), JC, CUR,
                      set(), ["specs/heldout*"]) == []
    assert tune.check({"action": "none", "changes": [], "why": "setup bug"}, JC, CUR, set(), []) == []


def test_apply_knobs_rewrites_assignments_flags_and_exports():
    out = tune.apply_knobs(JOB, {"PATIENCE": "5", "lr": "0.0005", "DART_NOISE": "0.1 0.2"}, KNOBS)
    assert "PATIENCE=5 REC_ENVS=1024 bash train.sh --lr 0.0005" in out
    assert out.splitlines()[3] == "export DART_NOISE='0.1 0.2'"     # after the #@ header block
    assert tune.current(out, "DART_NOISE", KNOBS["DART_NOISE"]) == "0.1 0.2"


TUNE = {"enabled": True, "max_per_job": 2, "max_per_day": 6, "points": 10,
        "triggers": {"stall": False, "plateau": {"window": 4, "eps": 0.02}}, "jobs": [JC]}


def plateau(box, name="imit", vals=(60, 61, 61, 60)):
    add(box, "fill_ledger.txt", *[f"2026-11-10 10:{10 + i}:00 EVAL {name} eval {v}%" for i, v in enumerate(vals)])


def test_plateau_triggers_tune_and_applies_a_checked_resubmit(proj, box, fake):
    cfg = setup(proj, TUNE)
    start(proj, cfg, box)
    fake({"tune": [ans(("EPOCHS", "40")), ans(("PATIENCE", "5"), ("DART_NOISE", "0.1 0.2"))]})
    plateau(box)
    got = tick(proj, cfg)
    assert got["tuned"] == ["imit: tune imit_t1"]
    sent = (box / "gq/pending/imit_t1.job").read_text()               # pushed in the same run
    assert "PATIENCE=5 REC_ENVS=1024" in sent and "export DART_NOISE='0.1 0.2'" in sent
    t = [r for r in ledger.entries(proj, kind="tune")][-1]
    assert t["name"] == "imit_t1" and t["change"] == "DART_NOISE=0.1 0.2 PATIENCE=5" and t["parent"] == "imit"
    runs = sorted((json.loads(f.read_text()) for f in proj.runs.glob("*.json")), key=lambda r: r["ts"])
    assert [r["outcome"] for r in runs if r["role"] == "tune"] == ["rejected", "ok"]
    assert "still running" in proj.owner_queue.read_text()
    plateau(box, vals=(60, 60, 61, 60))                                # same trigger: no second call
    assert tick(proj, cfg)["tuned"] == []


def test_rejected_answers_escalate_then_go_to_owner_queue(proj, box, fake):
    cfg = setup(proj, {**TUNE, "max_tier": "T3"})
    start(proj, cfg, box)
    fake({"tune": [ans(("PATIENCE", "99"))]})
    plateau(box)
    assert tick(proj, cfg)["tuned"] == ["imit: tune: rejected -> owner queue"]
    runs = sorted((json.loads(f.read_text()) for f in proj.runs.glob("*.json")), key=lambda r: r["ts"])
    tiers = [r["tier"] for r in runs]
    assert tiers == ["T2", "T2", "T3", "T3"]
    assert "tune answer for imit rejected" in proj.owner_queue.read_text()
    assert not list((box / "gq/pending").glob("imit_t*"))


def test_max_tier_stops_escalation(proj, box, fake):
    cfg = setup(proj, {**TUNE, "max_tier": "T2"})
    start(proj, cfg, box)
    fake({"tune": [ans(("PATIENCE", "99"))]})
    plateau(box)
    tick(proj, cfg)
    assert [json.loads(f.read_text())["tier"] for f in proj.runs.glob("*.json")] == ["T2", "T2"]


def test_per_job_and_per_day_caps(proj, box, fake):
    cfg = setup(proj, {**TUNE, "max_per_job": 1, "max_per_day": 1})
    for n in ("imit", "imitb"):
        start(proj, cfg, box, n)
    fake({"tune": {"action": "none", "changes": [], "why": "setup bug"}})
    plateau(box, "imit")
    plateau(box, "imitb")
    got = tick(proj, cfg)["tuned"]
    assert got == ["imit: tune: no change"]                           # imitb: day cap
    calls = [r for r in ledger.entries(proj, kind="tune") if r.get("called")]
    assert len(calls) == 1


def test_failed_tuned_run_joins_do_not_repeat(proj, box, fake):
    cfg = setup(proj, TUNE)
    start(proj, cfg, box)
    fake({"tune": ans(("PATIENCE", "5"))})
    plateau(box)
    tick(proj, cfg)
    add(box, "fill_ledger.txt", "2026-11-10 12:00:00 GQ start imit_t1.job on GPU 1",
        "2026-11-10 13:00:00 GQ CONTRACT FAIL imit_t1.job rc=0: success ev: 0.6 < 0.9")
    tick(proj, cfg)
    assert any("PATIENCE=5" in x and "imit" in x for x in ledger.do_not_repeat(proj))
    errs = tune.check(ans(("PATIENCE", "5")), JC, CUR,
                      {r["change"].lower() for r in ledger.entries(proj, kind="tune") if r.get("change")}, [])
    assert any("already tried" in e for e in errs)


def test_ended_job_on_a_plateau_is_tuned_not_diagnosed(proj, box, fake):
    cfg = setup(proj, TUNE, dispatch={"auto_diagnose": True})
    start(proj, cfg, box)
    fake({"tune": ans(("PATIENCE", "5")), "diagnose": None})
    plateau(box)
    add(box, "fill_ledger.txt", "2026-11-10 11:00:00 GQ CONTRACT FAIL imit.job rc=0: success ev: 0.6 < 0.9")
    out = tick(proj, cfg)["dispatch"]
    assert out[0]["action"] == "tune" and out[0]["applied"] == "tune imit_t1"
    assert not [f for f in proj.runs.glob("*.json") if json.loads(f.read_text())["role"] == "diagnose"]


@pytest.mark.parametrize("bad,msg", [
    ({"jobs": [{"match": "x", "knobs": {"bad name": {}}}]}, "env var name"),
    ({"jobs": [{"match": "x", "knobs": {"A": {"type": "list"}}}]}, "type must be"),
    ({"jobs": [{"match": "x", "knobs": {"A": {"type": "choice"}}}]}, "needs choices"),
    ({"jobs": [{"match": "x", "knobs": {"A": {"type": "int", "min": 5, "max": 1}}}]}, "min > max"),
    ({"jobs": [{"match": "x", "knobs": {}}]}, "at least one knob"),
    ({"triggers": {"vibes": {}}}, "unknown trigger"),
    ({"max_tier": "T9"}, "max_tier"),
])
def test_bad_tune_config_is_rejected(proj, bad, msg):
    raw = yaml.safe_load(proj.config_path.read_text())
    raw["tune"] = bad
    proj.config_path.write_text(yaml.safe_dump(raw))
    with pytest.raises(config.ConfigError, match=msg):
        config.load(proj)


# ── agent usage in tw spend ─────────────────────────────────────────────────
def _msg(mid, ts, model="claude-sonnet-5-5", out=100):
    return json.dumps({"timestamp": ts, "message": {"id": mid, "model": model, "usage": {
        "input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 5000,
        "output_tokens": out}}})


def test_spend_reports_subagent_tokens_by_type_and_day(proj, tmp_path, capsys):
    td = tmp_path / "transcripts"
    sa = td / "sess1" / "subagents"
    sa.mkdir(parents=True)
    (td / "sess1.jsonl").write_text(_msg("m0", "2026-11-10T01:00:00Z", "claude-opus-5-5") + "\n")
    (sa / "agent-a.jsonl").write_text("\n".join([_msg("m1", "2026-11-10T02:00:00Z"),
                                                 _msg("m1", "2026-11-10T02:00:00Z"),   # same msg, 2 blocks
                                                 _msg("m2", "2026-11-10T02:01:00Z")]) + "\n")
    (sa / "agent-a.meta.json").write_text(json.dumps({"agentType": "box-ops"}))
    (sa / "agent-b.jsonl").write_text(_msg("m3", "2026-11-10T03:00:00Z", "claude-haiku-5-5") + "\n")
    (sa / "agent-b.meta.json").write_text(json.dumps({"agentType": "box-status"}))
    st = usage.scan(td, days=36500, pricing=config.DEFAULTS["pricing"])
    assert st[("2026-11-10", "box-ops")]["msgs"] == 2 and st[("2026-11-10", "box-ops")]["output_tokens"] == 200
    assert st[("2026-11-10", "box-status")]["agents"] == 1 and ("2026-11-10", "(main)") in st
    assert usage.scan(td, 36500, main=False).get(("2026-11-10", "(main)")) is None
    assert cli.main(["spend", "--days", "36500", "--transcripts", str(td)]) == 0
    out = capsys.readouterr().out
    assert "box-ops" in out and "box-status" in out and "by type:" in out


# ── builder by step tier ────────────────────────────────────────────────────
@pytest.mark.parametrize("tier,agent,model", [("T1", "tw-builder-t1", "haiku"), ("T2", "tw-builder-t2", "sonnet"),
                                              ("T3", "tw-builder-t3", "opus"), (None, "tw-builder-t3", "opus"),
                                              ("T0", "tw-builder-t1", "haiku")])
def test_builder_follows_step_tier(proj, tier, agent, model):
    assert steps.builder({"tier": tier}) == (agent, model)
    f = proj.root / ".claude" / "agents" / f"{agent}.md"
    assert f.exists() and f"model: {model}" in f.read_text()


def test_step_show_names_the_builder(proj, capsys):
    (proj.steps_dir / "001-a.yaml").write_text(yaml.safe_dump(
        {"id": "a", "goal": "g", "why": "w", "changes": [], "gate": ["true"], "tier": "T2",
         "budget_usd": 1, "depends_on": [], "status": "open"}))
    assert cli.main(["step", "show"]) == 0
    assert capsys.readouterr().out.startswith("# builder: tw-builder-t2 (sonnet, step tier T2)")
    cmd = (proj.root / ".claude/commands/tw-step.md").read_text()
    assert "# builder:" in cmd and "Agent" in cmd


def test_init_templates_adds_missing_agents_only(proj):
    f = proj.root / ".claude/agents/tw-builder-t2.md"
    f.unlink()
    (proj.root / ".claude/agents/tw-builder-t1.md").write_text("edited")
    assert cli.main(["init", "--templates"]) == 0
    assert f.exists() and (proj.root / ".claude/agents/tw-builder-t1.md").read_text() == "edited"


def test_watch_adopts_a_job_queued_on_the_box_by_its_own_script(proj, box):
    cfg = setup(proj)
    (box / "gq/running/gq_own.job").write_text(JOB)
    add(box, "fill_ledger.txt", "2026-11-10 10:05:00 EVAL gq_own eval 70%")     # before adoption
    with pytest.raises(watch.WatchError, match="no job"):
        watch.add(proj, cfg, "not_there", "ended")
    assert watch.add(proj, cfg, "gq_own.job", "success>=0.9")["job"] == "gq_own"
    add(box, "gq/logs/gq_own.job.log", "step 1 loss 0.4")
    add(box, "fill_ledger.txt", "2026-11-10 10:10:00 EVAL gq_own eval 95%")
    got = tick(proj, cfg)
    assert got["trends"]["gq_own"]["series"]["loss"]["last"] == 0.4
    assert got["trends"]["gq_own"]["series"]["ev"]["n"] == 2            # backfilled + new
    assert got["fired"][0]["job"] == "gq_own"


def test_local_queue_trend_reaches_the_brief(proj, cfg):
    jf = job(proj, "loc", "#@ value loss re='loss (\\S+)' finite\n#@ progress n re='loss' stall=1h\n"
                          "for x in 0.9 0.6 0.4; do echo loss $x; done\n")
    from tierwright import jobqueue
    jobqueue.submit(proj, jf, require_contract=True, ext=config.contract_ext(cfg))
    assert cli.main(["queue", "run"]) == 0
    b = brief.build(proj, "loc")
    assert b["trend"]["series"]["loss"]["last"] == 0.4 and b["trend"]["state"] == "ended"


# ── adopt by glob ───────────────────────────────────────────────────────────
def test_start_lines_matching_adopt_globs_are_tracked_others_not(proj, box):
    cfg = setup(proj, queue={"backend": "ssh", "host": "ubuntu@fakebox", "adopt": ["dfp_*"]})
    remote.Remote(proj, cfg).pull()                                   # sets the offset
    (box / "gq/running/dfp_a.job").write_text(JOB)
    (box / "gq/running/other_b.job").write_text(JOB)
    add(box, "fill_ledger.txt", "2026-11-10 10:00:00 GQ start dfp_a.job on GPU 0",
        "2026-11-10 10:00:00 GQ start other_b.job on GPU 1",
        "2026-11-10 10:10:00 EVAL dfp_a eval 50%")
    got = tick(proj, cfg)
    tr = remote.Remote(proj, cfg).tracked()
    assert "dfp_a" in tr and "other_b" not in tr
    assert got["trends"]["dfp_a"]["success"] == 0.5
    add(box, "fill_ledger.txt", "2026-11-10 11:00:00 GQ CONTRACT FAIL dfp_a.job rc=0: success ev: 0.5 < 0.9")
    got = tick(proj, cfg)
    assert got["dispatch"][0]["job"] == "dfp_a"                       # verdict reaches the rules
    assert json.loads((proj.q("done") / "dfp_a.json").read_text())["contract"]["pass"] is False


def test_jobs_already_running_are_adopted_and_default_globs_come_from_tune(proj, box):
    cfg = setup(proj, TUNE)                                           # tune match imit*, no queue.adopt
    assert remote.Remote(proj, cfg).adopt_globs() == ["imit*"]
    remote.Remote(proj, cfg).pull()
    (box / "gq/running/imit_live.job").write_text(JOB)
    (box / "gq/running/zzz.job").write_text(JOB)
    tick(proj, cfg)
    assert set(remote.Remote(proj, cfg).tracked()) == {"imit_live"}


def test_bad_adopt_config_is_rejected(proj):
    with pytest.raises(config.ConfigError, match="queue.adopt"):
        setup(proj, queue={"backend": "ssh", "host": "ubuntu@fakebox", "adopt": "dfp_*"})


def test_watch_accepts_a_glob_and_fires_per_job(proj, box, monkeypatch):
    monkeypatch.setattr(watch, "_pushover_creds", lambda cfg: {})
    cfg = setup(proj, queue={"backend": "ssh", "host": "ubuntu@fakebox", "adopt": []})
    remote.Remote(proj, cfg).pull()
    (box / "gq/running/dfp_x.job").write_text(JOB)
    with pytest.raises(watch.WatchError, match="bad job glob"):
        watch.add(proj, cfg, "dfp_*;rm", "ended")
    assert cli.main(["watch", "dfp_*", "--until", "success>=0.9|ended"]) == 0
    assert "dfp_x" in remote.Remote(proj, cfg).tracked()              # running match adopted now
    add(box, "fill_ledger.txt", "2026-11-10 10:00:00 GQ start dfp_y.job on GPU 2",
        "2026-11-10 10:10:00 EVAL dfp_x eval 95%")
    (box / "gq/running/dfp_y.job").write_text(JOB)
    fired = tick(proj, cfg)["fired"]
    assert [f["job"] for f in fired] == ["dfp_x"]
    assert "dfp_y" in remote.Remote(proj, cfg).tracked()              # the glob joins adopt
    assert tick(proj, cfg)["fired"] == [] and "dfp_*" in watch.load(proj)
