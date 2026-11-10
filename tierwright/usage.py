"""Claude Code token use from this project's transcripts, so the savings of
moving work into code are measurable (`tw spend`).

    ~/.claude/projects/<project>/<session>.jsonl                   main sessions
    ~/.claude/projects/<project>/<session>/subagents/agent-*.jsonl subagents; the
        agent type is in agent-*.meta.json (`agentType`)

Each assistant message's `usage` counts once (a message split over several
lines repeats its id). Cost is estimated from `pricing:` at API rates; on a
subscription it is what the same tokens would cost, not a bill.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .backends import price

FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def _agent_type(f: Path) -> str:
    m = f.with_name(f.name.removesuffix(".jsonl") + ".meta.json")
    try:
        return json.loads(m.read_text()).get("agentType") or "?"
    except (OSError, json.JSONDecodeError):
        return "?"


def files(tdir: Path, days: float, main: bool = True) -> list[tuple[Path, str]]:
    cut = time.time() - days * 86400
    out = []
    if not tdir.exists():
        return out
    for f in tdir.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < cut:
                continue
        except OSError:
            continue
        if f.parent.name == "subagents" and f.name.startswith("agent-"):
            out.append((f, _agent_type(f)))
        elif main and f.parent == tdir:
            out.append((f, "(main)"))
    return out


def scan(tdir: Path, days: float = 7, pricing: dict | None = None, main: bool = True) -> dict:
    """{(day, agent_type): {"msgs", "agents", tokens..., "cost_usd"}}"""
    cut = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    out: dict = {}
    seen: set = set()
    for f, kind in files(tdir, days, main):
        agents_here = set()
        try:
            fh = open(f, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                    continue
                mid = msg.get("id") or d.get("uuid")
                if mid in seen:
                    continue
                seen.add(mid)
                day = (d.get("timestamp") or "")[:10] or time.strftime(
                    "%Y-%m-%d", time.gmtime(f.stat().st_mtime))
                if day < cut:
                    continue
                u = msg["usage"]
                s = out.setdefault((day, kind), {"msgs": 0, "agents": 0, "cost_usd": 0.0,
                                                 **{k: 0 for k in FIELDS}})
                s["msgs"] += 1
                for k in FIELDS:
                    s[k] += int(u.get(k) or 0)
                s["cost_usd"] += price(pricing or {}, msg.get("model", ""), u)
                if (day, kind) not in agents_here:
                    agents_here.add((day, kind))
                    s["agents"] += 1
    return out


def _k(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def lines(stats: dict) -> list[str]:
    if not stats:
        return ["agents: no Claude Code transcripts in range"]
    out = ["agents (Claude Code transcripts; $ at API rates):"]
    tot: dict = {}
    for (day, kind), s in sorted(stats.items()):
        out.append(f"  {day} {kind:16} {s['agents']:3} runs {s['msgs']:5} msgs  "
                   f"in {_k(s['input_tokens'] + s['cache_creation_input_tokens'])} "
                   f"cache {_k(s['cache_read_input_tokens'])} out {_k(s['output_tokens'])}  ~${s['cost_usd']:.2f}")
        t = tot.setdefault(kind, {"cost_usd": 0.0, "agents": 0, "out": 0})
        t["cost_usd"] += s["cost_usd"]
        t["agents"] += s["agents"]
        t["out"] += s["output_tokens"]
    out.append("  by type: " + ", ".join(f"{k} ~${v['cost_usd']:.2f}"
                                         for k, v in sorted(tot.items(), key=lambda kv: -kv[1]["cost_usd"])))
    return out
