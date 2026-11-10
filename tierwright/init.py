"""`tw init`: set up a project. Asks the backend (subscription / API / both) and
budgets, writes the config, goal, role prompts, subagents, guard hooks and
CLAUDE.md section, then probes each chosen backend with one tiny call."""

from __future__ import annotations

import json
import shlex
import sys
from importlib import resources
from pathlib import Path

from . import config, router
from .project import IGNORED, STATE, Project, now

MARK = "## Tierwright (read first)"
HOOK_CMD = f"{shlex.quote(sys.executable)} -m tierwright.hooks"  # the interpreter tierwright is installed in


def _tpl(rel: str) -> str:
    return resources.files("tierwright").joinpath(f"templates/{rel}").read_text()


def _ask(prompt: str, choices: list[str] | None = None, default: str = "") -> str:
    while True:
        hint = f" [{'/'.join(choices)}]" if choices else ""
        d = f" (default {default})" if default else ""
        ans = input(f"{prompt}{hint}{d}: ").strip() or default
        if not choices or ans in choices:
            return ans
        print(f"  choose one of {choices}")


def _merge_settings(path: Path) -> bool:
    cur = json.loads(path.read_text()) if path.exists() else {}
    pre = cur.setdefault("hooks", {}).setdefault("PreToolUse", [])
    for entry in pre:
        for h in entry.get("hooks", []):
            if "tierwright.hooks" in h.get("command", ""):
                return False
    pre.append({"matcher": "Bash|Read|Edit|Write|MultiEdit|NotebookEdit",
                "hooks": [{"type": "command", "command": HOOK_CMD}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur, indent=2) + "\n")
    return True


def templates(p: Project) -> list[str]:
    """Install the role prompts, subagents and slash commands that are missing.
    Existing files (possibly edited) are never overwritten."""
    wrote = []
    p.ensure()
    for f in resources.files("tierwright").joinpath("templates/roles").iterdir():
        dst = p.roles / f.name
        if not dst.exists():
            dst.write_text(f.read_text())
            wrote.append(f"{STATE}/roles/{f.name}")
    agents = p.root / ".claude" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    for f in resources.files("tierwright").joinpath("templates/agents").iterdir():
        dst = agents / f.name
        if not dst.exists():
            dst.write_text(f.read_text())
            wrote.append(f".claude/agents/{f.name}")
    cmds = p.root / ".claude" / "commands"
    cmds.mkdir(parents=True, exist_ok=True)
    added = []
    for f in resources.files("tierwright").joinpath("templates/commands").iterdir():
        dst = cmds / f.name
        if not dst.exists():
            dst.write_text(f.read_text())
            added.append("/" + f.name.removesuffix(".md"))
    if added:
        wrote.append("slash commands " + " ".join(added))
    return wrote


def run(root: str, backend: str | None = None, per_day: float | None = None,
        per_goal: float | None = None, goal: str | None = None, done: list[str] | None = None,
        probe: bool = True, force: bool = False, interactive: bool | None = None,
        lite: bool = False) -> dict:
    p = Project(root)
    interactive = sys.stdin.isatty() if interactive is None else interactive
    if p.config_path.exists() and not force:
        raise config.ConfigError(f"{p.config_path} exists (use --force to rewrite it)")
    if backend is None:
        if not interactive:
            raise config.ConfigError("--backend subscription|api|both is required when not interactive")
        if lite:
            print("Tierwright lite: the right-sized Claude model for each task, a budget, and a check\n"
                  "that proves each piece of work is done.\n")
        print("Where should agents run?\n"
              "  subscription  headless `claude -p` on your Claude plan (no per-token bill)\n"
              "  api           Anthropic API key, pay per token; batch + caching for high volume\n"
              "  both          builders on the plan, high-volume roles on the API")
        backend = _ask("backend", list(config.BACKENDS), "subscription")
    if backend not in config.BACKENDS:
        raise config.ConfigError(f"backend must be one of {config.BACKENDS}")
    if per_day is None:
        per_day = float(_ask("daily budget, USD", default="10")) if interactive else 10.0
    if per_goal is None:
        per_goal = float(_ask("budget for this goal, USD", default="50")) if interactive else 50.0
    if goal is None and interactive and not p.goal_path.exists():
        goal = _ask("goal, one sentence (blank: edit goal.yaml later)", default="") or None
        if goal and not done:
            if lite:
                out = _ask("where will the finished result be saved (a file path)", default="out/result.md")
                done = [f"file: {out}"]
                guide = _ask("a guide it must follow, e.g. brand.md (blank: none)", default="")
                if guide:
                    done.append(f"review: {out} against {guide}")
            else:
                d = _ask("done bar: a shell command that exits 0 only when done", default="true")
                done = [d]

    p.ensure()
    wrote = []
    config.write_default(p.config_path, backend, per_day, per_goal, "lite" if lite else "full")
    wrote.append("tierwright.yaml")
    if not p.goal_path.exists():
        if goal:
            import yaml
            p.goal_path.write_text(yaml.safe_dump({"goal": goal, "done": done or ["true"],
                                                   "out_of_scope": []}, sort_keys=False))
        else:
            p.goal_path.write_text(_tpl("goal.yaml"))
        wrote.append("goal.yaml")
    (p.state / "goal.started").write_text(str(now()))
    wrote.append(f"{STATE}/roles/")
    wrote += templates(p)
    if _merge_settings(p.root / ".claude" / "settings.json"):
        wrote.append(".claude/settings.json (guard hooks)")
    cm = p.root / "CLAUDE.md"
    section = _tpl("CLAUDE.lite.md" if lite else "CLAUDE.section.md")
    if not cm.exists():
        cm.write_text(f"# CLAUDE.md\n\n{section}")
        wrote.append("CLAUDE.md")
    elif MARK not in cm.read_text():
        cm.write_text(cm.read_text().rstrip() + "\n\n" + section)
        wrote.append("CLAUDE.md (section appended)")
    gi = p.root / ".gitignore"
    have = gi.read_text().splitlines() if gi.exists() else []
    add = [f"{STATE}/{x}" for x in IGNORED if f"{STATE}/{x}" not in have]
    if add:
        with open(gi, "a") as f:
            f.write(("\n" if have and have[-1] else "") + "# tierwright runtime state\n" + "\n".join(add) + "\n")
        wrote.append(".gitignore")

    probes = {}
    if probe:
        cfg = config.load(p)
        for b in (["subscription", "api"] if backend == "both" else [backend]):
            probes[b] = router.probe(p, cfg, b) or "ok"
    return {"root": str(p.root), "backend": backend, "mode": "lite" if lite else "full",
            "wrote": wrote, "probes": probes}
