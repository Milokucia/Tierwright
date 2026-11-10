"""Claude Code PreToolUse guardrails, configured by `tw init`.

    python3 -m tierwright.hooks      reads the hook JSON on stdin

Exit 2 blocks the tool call and the message on stderr goes back to the agent.
Rules (each switchable in tierwright.yaml `hooks:`):
    block_polling     sleep >= 20s, watch, tail -f, sleep-in-a-loop: agents do
                      not wait on jobs; `tw watch JOB --until COND` registers the
                      wait and the dispatcher checks it
    block_raw_logs    reading .tierwright/logs/*: use `tw brief JOB`
    protected_paths   Edit/Write on a glob listed there (held-out sets, graders)
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys

import yaml

POLL = [
    (re.compile(r"\bsleep\s+(\d+)"), lambda m: int(m.group(1)) >= 20),
    (re.compile(r"(?:^|[;&|(]|\bthen|\bdo)\s*watch\s", re.M), lambda m: True),
    (re.compile(r"\btail\s+(-\w*f|--follow)"), lambda m: True),
    (re.compile(r"\b(while|until)\b.*\bsleep\b", re.S), lambda m: True),
]
RAW_LOG = re.compile(r"\.tierwright/logs/")
TW_CMD = re.compile(r"(^|[\s;&|(])(tw|tierwright)\s")


def _cfg(cwd: str) -> dict:
    d = os.path.abspath(cwd)
    while True:
        f = os.path.join(d, "tierwright.yaml")
        if os.path.exists(f):
            try:
                return (yaml.safe_load(open(f)) or {}).get("hooks", {}) or {}
            except yaml.YAMLError:
                return {}
        nd = os.path.dirname(d)
        if nd == d:
            return {}
        d = nd


def decide(event: dict) -> str | None:
    """The block message, or None to allow."""
    tool = event.get("tool_name", "")
    inp = event.get("tool_input", {}) or {}
    cwd = event.get("cwd") or os.getcwd()
    h = {"block_polling": True, "block_raw_logs": True, "protected_paths": []}
    h.update(_cfg(cwd))
    if tool == "Bash":
        cmd = inp.get("command", "")
        if h.get("block_polling"):
            for rx, bad in POLL:
                m = rx.search(cmd)
                if m and bad(m):
                    return ("Tierwright: polling is blocked: use tw watch. Register the condition with "
                            "`tw watch JOB --until 'success>=0.9|stalled|ended'` and end your turn; the "
                            "dispatcher checks it on every `tw queue run` and writes an attention file "
                            "and a Pushover alert when it fires. Read `tw brief JOB` once for the trend.")
        if h.get("block_raw_logs") and RAW_LOG.search(cmd) and not TW_CMD.search(cmd):
            return "Tierwright: read `tw brief JOB` (a few KB of JSON), not raw logs."
    if tool in ("Read",) and h.get("block_raw_logs") and RAW_LOG.search(inp.get("file_path", "")):
        return "Tierwright: read `tw brief JOB` (a few KB of JSON), not raw logs."
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        rel = os.path.relpath(path, cwd) if os.path.isabs(path) else path
        for g in h.get("protected_paths") or []:
            if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(path, g):
                return (f"Tierwright: {rel} is protected ({g}): graders and held-out sets are never "
                        "edited by agents. Put the change in owner-queue.md.")
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    msg = decide(event)
    if msg:
        print(msg, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
