"""`tw watch JOB --until COND`: register a condition, return at once. The
dispatcher (`tw queue run`, e.g. a 5-minute timer) checks it on every run; no
agent waits.

    COND   alternatives joined by `|`, each one of:
             ended | stalled
             METRIC OP NUMBER      OP: >= <= > < ==
           METRIC: success (the first `#@ success` series), a `#@ value` /
           `#@ success` / `#@ progress` name, NAME.slope (per hour), NAME.peak,
           NAME.drop (peak - last), NAME.n, or age_h
    e.g.   'success>=0.9|stalled|ended'   'loss.slope>=0|age_h>6'

When a clause holds, the watch fires once: .tierwright/attention/watch-<job>.json
(the clause, the trend), a ledger line, and a Pushover message when
PUSHOVER_TOKEN / PUSHOVER_USER are set (env, or KEY=VALUE lines in ~/.pushover).
A job that ends with no clause true also fires ("ended; COND not met"), so a
watch never outlives its job. On an ssh queue a job queued on the host by its
own script (not `tw submit`) is adopted: its file is read from the running or
pending dir, and from then on it is tracked like any other. JOB may be a glob
('dfp_*'): each matching job fires once and the watch stays; on an ssh queue
the glob also joins queue.adopt.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

from . import ledger, trend
from .project import Project, ProjectError, clip, now, write_json

CLAUSE = re.compile(r"^\s*(?:(ended|stalled)|([A-Za-z_]\w*(?:\.(?:last|slope|peak|drop|n))?)"
                    r"\s*(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?(?:e-?\d+)?))\s*$")
OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
       "<": lambda a, b: a < b, "==": lambda a, b: a == b}


class WatchError(ProjectError):
    pass


def parse(cond: str) -> list[tuple]:
    """'success>=0.9|stalled' -> [("metric", "success", ">=", 0.9), ("stalled",)]; raises on bad input."""
    out = []
    if not cond or not cond.strip():
        raise WatchError("--until needs a condition, e.g. 'success>=0.9|stalled|ended'")
    for part in cond.split("|"):
        m = CLAUSE.match(part)
        if not m:
            raise WatchError(f"bad clause {part.strip()!r}: use ended, stalled, or METRIC OP NUMBER "
                             "(e.g. success>=0.9, loss.slope>=0)")
        if m.group(1):
            out.append((m.group(1),))
        else:
            out.append(("metric", m.group(2), m.group(3), float(m.group(4))))
    return out


def _f(p: Project) -> Path:
    return p.state / "watches.json"


def load(p: Project) -> dict:
    f = _f(p)
    return json.loads(f.read_text()) if f.exists() else {}


def _save(p: Project, w: dict) -> None:
    write_json(_f(p), w)


def known(p: Project, cfg: dict, job: str) -> bool:
    if (p.q("remote") / f"{job}.json").exists():
        return True
    return any((p.q(part) / f"{job}.{ext}").exists()
               for part in ("pending", "running", "done") for ext in ("job", "json"))


GLOB_CH = set("*?[")
GLOB_RE = re.compile(r"^[A-Za-z0-9_.*?\[\]!-]+$")


def is_glob(s: str) -> bool:
    return bool(GLOB_CH & set(s))


def globs(p: Project) -> list[str]:
    return [k for k in load(p) if not k.startswith("__") and is_glob(k)]


def add(p: Project, cfg: dict, job: str, cond: str) -> dict:
    parse(cond)
    job = job.removesuffix(".job")
    if is_glob(job):
        # a glob: every matching job (adopted on an ssh queue) fires once; the watch stays
        if not GLOB_RE.match(job):
            raise WatchError(f"bad job glob {job!r}")
        w = load(p)
        w.setdefault(job, [])
        if cond not in w[job]:
            w[job].append(cond)
        w.setdefault("__since__", {})[job] = now()
        _save(p, w)
        if (cfg.get("queue") or {}).get("backend") == "ssh":
            from . import remote
            remote.Remote(p, cfg).adopt_running()
        return {"job": job, "until": cond}
    if not known(p, cfg, job) and (cfg.get("queue") or {}).get("backend") == "ssh":
        from . import remote
        remote.Remote(p, cfg).adopt(job)        # queued on the box by its own script
    if not known(p, cfg, job):
        raise WatchError(f"no job {job!r} here or running / pending on the queue host")
    w = load(p)
    w.setdefault(job, [])
    if cond not in w[job]:
        w[job].append(cond)
    _save(p, w)
    return {"job": job, "until": cond}


def copy(p: Project, src: str, dst: str) -> None:
    """A tuned or resubmitted job inherits its parent's watches."""
    w = load(p)
    if src in w:
        w[dst] = list(dict.fromkeys(w.get(dst, []) + w[src]))
        _save(p, w)


def _ended(p: Project, job: str, tr: dict | None) -> bool:
    if tr and tr.get("state") == "ended":
        return True
    r = p.q("remote") / f"{job}.json"
    if r.exists():
        return json.loads(r.read_text()).get("state") == "ended"
    return (p.q("done") / f"{job}.json").exists()


def holds(clauses: list[tuple], tr: dict | None, ended: bool) -> str | None:
    for c in clauses:
        if c[0] == "ended" and ended:
            return "ended"
        if c[0] == "stalled" and tr and tr.get("stalled"):
            return "stalled"
        if c[0] == "metric":
            v = trend.metric(tr, c[1])
            if isinstance(v, (int, float)) and OPS[c[2]](v, c[3]):
                return f"{c[1]}{c[2]}{c[3]:g} (now {v:g})"
    return None


def check(p: Project, cfg: dict) -> list[dict]:
    """Fire every watch whose condition holds. Returns what fired."""
    w = load(p)
    fired, keep = [], {}
    done_map = dict(w.get("__fired__") or {})
    for pat, conds in w.items():
        if pat.startswith("__") or not is_glob(pat):
            continue
        keep[pat] = conds
        since = (w.get("__since__") or {}).get(pat, 0)
        for job in _matching(p, pat, since):
            tr = trend.compact(p, job)
            ended = _ended(p, job, tr)
            for cond in conds:
                k = f"{pat}|{cond}"
                if job in done_map.get(k, []):
                    continue
                why = holds(parse(cond), tr, ended) or (f"ended; {cond} not met" if ended else None)
                if why:
                    fired.append(fire(p, cfg, job, cond, why, tr))
                    done_map.setdefault(k, []).append(job)
    for k in ("__since__",):
        if k in w:
            keep[k] = w[k]
    if done_map:
        keep["__fired__"] = done_map
    for job, conds in w.items():
        if job.startswith("__") or is_glob(job):
            continue
        tr = trend.compact(p, job)
        ended = _ended(p, job, tr)
        left = []
        for cond in conds:
            why = holds(parse(cond), tr, ended)
            if not why and ended:
                why = f"ended; {cond} not met"
            if not why:
                left.append(cond)
                continue
            fired.append(fire(p, cfg, job, cond, why, tr))
        if left:
            keep[job] = left
    if keep != w:
        _save(p, keep)
    return fired


def _matching(p: Project, pat: str, since: float) -> list[str]:
    import fnmatch
    out = []
    for f in sorted(p.q("remote").glob("*.json")):
        try:
            r = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        if fnmatch.fnmatch(r.get("job", ""), pat) and (r.get("end") or now()) >= since:
            out.append(r["job"])
    for part in ("running", "done"):
        for f in sorted(p.q(part).glob("*.job")):
            if fnmatch.fnmatch(f.stem, pat) and f.stem not in out and f.stat().st_mtime >= since - 1:
                out.append(f.stem)
    return out


def fire(p: Project, cfg: dict, job: str, cond: str, why: str, tr: dict | None) -> dict:
    title = f"tw watch {job}: {why}"
    body = trend.line(job, tr) if tr else f"{job}: no trend yet"
    pushed = notify(cfg, title, body)
    att = {"kind": "watch", "job": job, "until": cond, "fired": why, "ts": now(), "trend": tr,
           "pushed": pushed}
    write_json(p.attention / f"watch-{job}.json", att)
    ledger.record(p, "watch", job, "unknown", note=f"{why} (until {cond})")
    return att


# ── notify ──────────────────────────────────────────────────────────────────
SENDER = None          # tests swap in a fake: f(url, data) -> None


def _pushover_creds(cfg: dict) -> dict:
    n = (cfg.get("notify") or {}).get("pushover") or {}
    tok_env, usr_env = n.get("token_env", "PUSHOVER_TOKEN"), n.get("user_env", "PUSHOVER_USER")
    c = {}
    if os.environ.get(tok_env):
        c["token"] = os.environ[tok_env]
    if os.environ.get(usr_env):
        c["user"] = os.environ[usr_env]
    f = Path(os.path.expanduser(n.get("file", "~/.pushover")))
    if len(c) < 2 and f.exists():
        for ln in f.read_text().splitlines():
            k, _, v = ln.strip().partition("=")
            v = v.strip().strip("'\"")
            if k == tok_env and v:
                c.setdefault("token", v)
            if k == usr_env and v:
                c.setdefault("user", v)
    return c if len(c) == 2 else {}


def notify(cfg: dict, title: str, msg: str) -> bool:
    """Pushover when credentials exist; False (and nothing sent) otherwise."""
    c = _pushover_creds(cfg)
    if not c:
        return False
    data = urllib.parse.urlencode({"token": c["token"], "user": c["user"], "title": clip(title, 250),
                                   "message": clip(msg, 1000)}).encode()
    url = "https://api.pushover.net/1/messages.json"
    try:
        if SENDER:
            SENDER(url, data)
        else:
            urllib.request.urlopen(url, data, timeout=15)
        return True
    except OSError:
        return False
