import json

import pytest

from conftest import job
from tierwright import config, dispatch, hooks, init, jobqueue, rules
from tierwright.project import Project


def _run(p, cfg):
    cfg["dispatch"]["auto_diagnose"] = False
    jobqueue.run(p, once=True, on_end=lambda m: dispatch.poll(p, cfg))


def test_rule_add_needs_its_incident_and_spares_ok_jobs(proj, cfg):
    jobqueue.submit(proj, job(proj, "good", "#@ forbid x re=NEVER\necho fine\n"))
    jobqueue.submit(proj, job(proj, "flaky", "#@ forbid x re=NEVER\necho 'mirror hiccup 42'\nexit 1\n"))
    _run(proj, cfg)
    too_wide = {"name": "wide", "re": ".", "action": "ignore", "class": "x", "incident": "i"}
    assert not rules.add(proj, too_wide)["ok"]                       # would flip `good`
    miss = {"name": "m", "re": "never-seen", "action": "resubmit", "class": "net", "incident": "i"}
    assert not rules.add(proj, miss, incident_job="flaky")["ok"]     # does not match its incident
    good = {"name": "mirror", "re": "mirror hiccup", "action": "resubmit", "class": "net", "incident": "i"}
    assert rules.add(proj, good, incident_job="flaky")["ok"]
    assert rules.load(proj)[0].name == "mirror"


def test_rule_validation():
    with pytest.raises(ValueError):
        rules.from_dict({"name": "x", "action": "explode", "class": "c", "incident": "i", "re": "a"})
    with pytest.raises(ValueError):
        rules.from_dict({"name": "x", "action": "ok", "class": "c", "incident": "i"})


@pytest.mark.parametrize("cmd,blocked", [
    ("sleep 300 && tw status", True),
    ("while true; do tw status; sleep 5; done", True),
    ("tail -f .tierwright/logs/x.log", True),
    ("cat .tierwright/logs/x.log", True),
    ("sleep 2", False),
    ("tw brief x", False),
    ("pytest -q", False),
])
def test_hook_bash(proj, cmd, blocked):
    ev = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(proj.root)}
    assert (hooks.decide(ev) is not None) == blocked


def test_hook_protected_path(proj):
    cfgf = proj.config_path
    cfgf.write_text(cfgf.read_text().replace("protected_paths: []", "protected_paths: ['heldout/*']"))
    ev = {"tool_name": "Write", "tool_input": {"file_path": str(proj.root / "heldout/a.json")},
          "cwd": str(proj.root)}
    assert "protected" in hooks.decide(ev)


def test_init_files_and_idempotent_hooks(proj):
    s = json.loads((proj.root / ".claude/settings.json").read_text())
    assert len(s["hooks"]["PreToolUse"]) == 1
    assert init._merge_settings(proj.root / ".claude/settings.json") is False
    assert (proj.root / ".claude/agents/tw-status.md").read_text().count("model: haiku") == 1
    assert "## Tierwright (read first)" in (proj.root / "CLAUDE.md").read_text()
    with pytest.raises(config.ConfigError, match="exists"):
        init.run(str(proj.root), backend="api", probe=False, interactive=False)


def test_init_installs_slash_commands(proj):
    cmds = sorted(f.name for f in (proj.root / ".claude/commands").glob("tw-*.md"))
    assert cmds == ["tw-adopt.md", "tw-brief.md", "tw-check.md", "tw-mine-apply.md", "tw-mine.md",
                    "tw-status.md", "tw-step.md"]
    for f in (proj.root / ".claude/commands").glob("tw-*.md"):
        head = f.read_text().split("---")[1]
        assert "description:" in head and "allowed-tools:" in head
    assert "model: haiku" in (proj.root / ".claude/commands/tw-status.md").read_text()
    # a user's edited command is never overwritten
    (proj.root / ".claude/commands/tw-status.md").write_text("mine")
    init.run(str(proj.root), backend="subscription", probe=False, interactive=False, force=True)
    assert (proj.root / ".claude/commands/tw-status.md").read_text() == "mine"


def test_init_appends_to_existing_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Mine\n\nkeep this\n")
    init.run(str(tmp_path), backend="both", probe=False, interactive=False)
    t = (tmp_path / "CLAUDE.md").read_text()
    assert t.startswith("# Mine") and "keep this" in t and "Tierwright (read first)" in t
    assert config.load(Project(tmp_path))["backend"] == "both"


def test_init_requires_backend_when_not_interactive(tmp_path):
    with pytest.raises(config.ConfigError, match="--backend"):
        init.run(str(tmp_path), probe=False, interactive=False)
