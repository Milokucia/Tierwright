"""Project paths and the small file helpers every module shares.

A Tierwright project is any directory holding `tierwright.yaml`. Its state:

    tierwright.yaml        backend, budgets, tiers, role overrides, hooks
    goal.yaml              the goal and its done bar
    steps/NNN-<id>.yaml    the step ladder (committed)
    STATUS.md, GAP.md      written by code, read by people and agents
    owner-queue.md         decisions only the owner makes
    .tierwright/
      ledger.jsonl         every attempt and verdict (committed)
      rules.yaml           project triage rules (committed)
      roles/               frozen role prompts (committed)
      queue/{pending,running,done}/, logs/, events.jsonl, briefs/,
      attention/, runs/, map.json, inventory.json,
      trends/, tune/, watches.json                    (ignored)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

CONFIG = "tierwright.yaml"
STATE = ".tierwright"
IGNORED = ["queue/", "logs/", "events.jsonl", "events.cursor", "briefs/",
           "attention/", "runs/", "map.json", "inventory.json", "mine.json",
           "trends/", "tune/", "watches.json"]


class ProjectError(Exception):
    pass


def find_root(start: str | os.PathLike | None = None) -> Path:
    p = Path(start or os.getcwd()).resolve()
    for d in [p, *p.parents]:
        if (d / CONFIG).exists():
            return d
    raise ProjectError(f"no {CONFIG} in {p} or its parents: run `tw init` first")


class Project:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root).resolve()
        self.state = self.root / STATE

    # paths -----------------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.root / CONFIG

    @property
    def goal_path(self) -> Path:
        return self.root / "goal.yaml"

    @property
    def steps_dir(self) -> Path:
        return self.root / "steps"

    def q(self, part: str) -> Path:
        return self.state / "queue" / part

    @property
    def logs(self) -> Path:
        return self.state / "logs"

    @property
    def events(self) -> Path:
        return self.state / "events.jsonl"

    @property
    def ledger(self) -> Path:
        return self.state / "ledger.jsonl"

    @property
    def runs(self) -> Path:
        return self.state / "runs"

    @property
    def attention(self) -> Path:
        return self.state / "attention"

    @property
    def briefs(self) -> Path:
        return self.state / "briefs"

    @property
    def roles(self) -> Path:
        return self.state / "roles"

    @property
    def owner_queue(self) -> Path:
        return self.root / "owner-queue.md"

    def ensure(self) -> "Project":
        for d in [self.q("pending"), self.q("running"), self.q("done"), self.logs,
                  self.runs, self.attention, self.briefs, self.roles, self.steps_dir]:
            d.mkdir(parents=True, exist_ok=True)
        return self


def now() -> float:
    return time.time()


def stamp(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t if t is not None else now()))


def append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def owner_note(p: Project, title: str, body: str) -> None:
    """Append a decision only the owner makes. Never silently dropped."""
    new = not p.owner_queue.exists()
    with open(p.owner_queue, "a") as f:
        if new:
            f.write("# Owner queue\n\nDecisions only the owner makes. Newest last; strike an "
                    "item through when decided.\n")
        f.write(f"\n* **{title}** ({stamp()} UTC). {body}\n")


def clip(s: str, n: int = 200) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 3] + "..."
