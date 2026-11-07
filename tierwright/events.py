"""Append-only event log with a read cursor, so a restart neither repeats nor
skips an event. The queue writes; the dispatcher reads."""

from __future__ import annotations

from .project import Project, append_jsonl, now, read_jsonl


def emit(p: Project, kind: str, job: str, **data) -> dict:
    rec = {"ts": now(), "kind": kind, "job": job, **data}
    append_jsonl(p.events, rec)
    return rec


def unread(p: Project) -> list[dict]:
    cur = p.state / "events.cursor"
    n = int(cur.read_text() or 0) if cur.exists() else 0
    evs = read_jsonl(p.events)
    return evs[n:]


def mark_read(p: Project, count: int) -> None:
    cur = p.state / "events.cursor"
    n = int(cur.read_text() or 0) if cur.exists() else 0
    cur.write_text(str(n + count))
