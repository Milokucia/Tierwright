"""Trends of running jobs, read by code so no agent watches a job.

    ingest(p, cfg, remote=None)   every `tw queue run`: read the bytes each
                                  running job wrote since the last run (byte
                                  offsets per source), apply its `#@ progress`,
                                  `#@ value` and `#@ success` regexes, keep the
                                  points in .tierwright/trends/<job>.json
    compact(p, job)               a few hundred bytes for the brief: last value,
                                  slope, peak, stall, success so far

Sources, from the job's own contract:
    (no in=)      the job's stdout: the local log, or on an ssh queue the box's
                  `queue.log_dir`/`queue.log_name` (null log_dir: skipped)
    in=ledger     the queue ledger lines for this job (.tierwright/logs/<job>.log,
                  written by the ssh pull)
    in=PATH       a file on the host (or local); `${VAR}` is filled from
                  `#@ var NAME re=RE` matches and the environment; globs skipped
`pct` divides a percent by 100. A source never read starts at byte 0.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

from . import contract
from .project import Project, clip, now, write_json

TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
MAX_POINTS = 200
READ_MAX = 4 * 2**20
SLOPE_POINTS = 10
VAR_RE = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def tdir(p: Project) -> Path:
    return p.state / "trends"


def _path(p: Project, job: str) -> Path:
    return tdir(p) / f"{job}.json"


def load(p: Project, job: str) -> dict | None:
    f = _path(p, job)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:
        return None


def _ts(line: str, default: float) -> float:
    m = TS_RE.match(line)
    if not m:
        return default
    import calendar
    import time
    return float(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")))


def _fill(s: str, vars_: dict) -> str | None:
    """`${VAR}` from the job's vars, then the environment; None if one is unknown."""
    miss = []

    def sub(m):
        name, default = m.group(1), m.group(2)
        if name in vars_:
            return vars_[name]
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        miss.append(name)
        return ""

    out = VAR_RE.sub(sub, s)
    return None if miss else out


def _checks(text: str, ext: dict) -> list[contract.Check]:
    lenient = {**(ext or {}), "unknown": "warn"}
    try:
        return contract.parse(text, lenient, [])
    except contract.ContractError:
        return []


def _num(m: re.Match, pct: bool) -> float | None:
    g = m.groups()
    try:
        v = float(g[0]) / float(g[1]) if len(g) >= 2 and g[1] is not None else float(g[0])
    except (IndexError, TypeError, ValueError, ZeroDivisionError):
        return None
    if pct and len(g) < 2:
        v /= 100.0
    return v


class Sources:
    """Reads new bytes of a job's sources: local files directly, host files in
    one ssh call per run."""

    def __init__(self, p: Project, cfg: dict, remote=None):
        self.p, self.cfg, self.remote = p, cfg, remote

    def read(self, want: dict[str, tuple[str, int]]) -> dict[str, tuple[bytes, int]]:
        """{key: (location, offset)} -> {key: (new bytes ending at a newline, new offset)}.
        location 'local:PATH' or 'host:PATH'."""
        out, host = {}, {}
        for k, (loc, off) in want.items():
            kind, path = loc.split(":", 1)
            if kind == "local":
                out[k] = _read_local(path, off)
            else:
                host[k] = (path, off)
        if host and self.remote is not None:
            out.update(_read_host(self.remote, host))
        return out


def _cut(data: bytes, off: int) -> tuple[bytes, int]:
    i = data.rfind(b"\n")
    if i < 0:
        return b"", off
    return data[:i + 1], off + i + 1


def _read_local(path: str, off: int) -> tuple[bytes, int]:
    try:
        with open(os.path.expanduser(path), "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size < off:
                off = 0
            f.seek(off)
            return _cut(f.read(min(READ_MAX, size - off)), off)
    except OSError:
        return b"", off


def _read_host(remote, want: dict[str, tuple[str, int]]) -> dict[str, tuple[bytes, int]]:
    from .remote import _rpath
    keys = list(want)
    parts = []
    for i, k in enumerate(keys):
        path, off = want[k]
        rp = _rpath(path)
        parts.append(
            f"s=$(stat -c %s {rp} 2>/dev/null || echo -1); o={off}; "
            f"if [ \"$s\" -ge 0 ] && [ \"$s\" -lt \"$o\" ]; then o=0; fi; "
            f"n=$(( s > o ? s - o : 0 )); [ $n -gt {READ_MAX} ] && n={READ_MAX}; "
            f"echo \"@@TW {i} $s $o $n\"; "
            f"if [ $n -gt 0 ]; then tail -c +$((o + 1)) {rp} | head -c $n; fi; echo")
    r = remote.ssh("; ".join(parts), timeout=300, text=False)
    data = r.stdout or b""
    out, pos = {}, 0
    head = re.compile(rb"@@TW (\d+) (-?\d+) (\d+) (\d+)\n")
    while True:
        m = head.search(data, pos)
        if not m:
            break
        i, size, o, n = (int(x) for x in m.groups())
        body = data[m.end(): m.end() + n]
        pos = m.end() + n
        if i < len(keys):
            out[keys[i]] = _cut(body, o) if size >= 0 else (b"", want[keys[i]][1])
    return out


def _sources_for(p: Project, cfg: dict, job: str, checks, vars_: dict, ssh: bool) -> dict:
    """check name -> list of 'local:PATH' / 'host:PATH' locations (None: unresolved)."""
    q = cfg.get("queue") or {}
    logs = [c.arg for c in checks if c.kind == "log"]
    if logs:
        default = _loc(logs[0], vars_, ssh, p)
    elif ssh:
        ld = q.get("log_dir")
        default = [f"host:{ld.rstrip('/')}/{q.get('log_name', '{job}.job.log').format(job=job)}"] if ld else []
    else:
        default = [f"local:{p.logs / (job + '.log')}"]
    out = {}
    for c in checks:
        if c.kind not in ("progress", "value", "success"):
            continue
        src = c.opts.get("in")
        out[c.name] = default if not src else _loc(src, vars_, ssh, p)
    return out


def _loc(src: str, vars_: dict, ssh: bool, p: Project):
    out = []
    for part in src.split(","):
        part = part.strip()
        if part == "ledger":
            out.append("ledger")
            continue
        s = _fill(part, vars_)
        if s is None or any(ch in s for ch in "*?["):
            continue
        if ssh:
            out.append(f"host:{s}")
        else:
            out.append(f"local:{s if os.path.isabs(os.path.expanduser(s)) else p.root / s}")
    return out


def _jobs(p: Project, cfg: dict, remote=None) -> dict[str, dict]:
    """job -> {"text", "state", "start"} for every job worth reading this run."""
    out = {}
    if remote is not None:
        for j, r in remote.tracked(open_only=False).items():
            st = r.get("state")
            cur = load(p, j)
            if st == "running" or (st == "ended" and cur is not None and cur.get("state") != "ended"):
                src = r.get("src")
                if src and Path(src).exists():
                    out[j] = {"text": Path(src).read_text(), "state": st, "start": r.get("start")}
            elif st == "ended" and cur is None and r.get("start"):
                src = r.get("src")
                if src and Path(src).exists():   # ended between two runs: one final read
                    out[j] = {"text": Path(src).read_text(), "state": "ended", "start": r.get("start")}
        return out
    for f in sorted(p.q("running").glob("*.job")):
        out[f.stem] = {"text": f.read_text(), "state": "running", "start": f.stat().st_mtime}
    for f in sorted(p.q("done").glob("*.json")):
        cur = load(p, f.stem)
        if cur is None or cur.get("state") != "ended":   # one final read after the end
            jf = p.q("done") / f"{f.stem}.job"
            if jf.exists():
                start = (cur or {}).get("start")
                if start is None:
                    try:
                        start = json.loads(f.read_text()).get("start")
                    except json.JSONDecodeError:
                        start = None
                out[f.stem] = {"text": jf.read_text(), "state": "ended", "start": start}
    return out


def ingest(p: Project, cfg: dict, remote=None) -> dict[str, dict]:
    """Read every running job's new output into its trend. Returns job -> compact trend."""
    from . import config
    ext = config.contract_ext(cfg)
    ssh = remote is not None
    reader = Sources(p, cfg, remote)
    jobs = _jobs(p, cfg, remote)
    plans, states = {}, {}
    for job, j in jobs.items():
        st = load(p, job) or {"job": job, "offsets": {}, "vars": {}, "series": {}, "progress": {},
                              "start": j.get("start") or now()}
        st["state"] = j["state"]
        checks = _checks(j["text"], ext)
        st["_checks"] = checks
        states[job] = st
        # vars first: they fill the regexes and paths below
        for c in checks:
            if c.kind == "var" and c.arg not in st["vars"] and c.opts.get("re"):
                plans.setdefault(job, set()).update(_loc(c.opts.get("in", "ledger"), st["vars"], ssh, p))
        srcs = _sources_for(p, cfg, job, checks, st["vars"], ssh)
        st["_srcs"] = srcs
        for locs in srcs.values():
            plans.setdefault(job, set()).update(locs)
    want = {}
    for job, locs in plans.items():
        for loc in locs:
            real = f"local:{p.logs / (job + '.log')}" if loc == "ledger" else loc
            want[f"{job}|{loc}"] = (real, int(states[job]["offsets"].get(loc, 0)))
    got = reader.read(want) if want else {}
    t_now = now()
    out = {}
    for job, st in states.items():
        checks, srcs = st.pop("_checks"), st.pop("_srcs")
        texts = {}
        for loc in plans.get(job, ()):
            data, off = got.get(f"{job}|{loc}", (b"", st["offsets"].get(loc, 0)))
            st["offsets"][loc] = off
            texts[loc] = data.decode("utf-8", "replace").splitlines()
        for c in checks:
            if c.kind == "var" and c.arg not in st["vars"] and c.opts.get("re"):
                for loc in _loc(c.opts.get("in", "ledger"), st["vars"], ssh, p):
                    for line in texts.get(loc, []):
                        m = re.search(c.opts["re"], line)
                        if m and m.groups():
                            st["vars"][c.arg] = m.group(1)
                            break
        for c in checks:
            if c.kind not in ("progress", "value", "success"):
                continue
            rx = _fill(c.opts.get("re", ""), st["vars"]) if c.opts.get("re") else None
            if c.opts.get("re") and rx is None:
                continue
            lines = [ln for loc in srcs.get(c.name, []) for ln in texts.get(loc, [])]
            if c.kind == "progress":
                _progress(st, c, rx, lines, t_now)
            else:
                _series(st, c, rx, lines, t_now)
        if st["state"] == "ended":
            st["ended"] = t_now
        write_json(_path(p, job), st)
        out[job] = compact_state(st, t_now)
    return out


def _progress(st: dict, c, rx: str | None, lines: list[str], t: float) -> None:
    pr = st["progress"].setdefault(c.name, {"last": None, "changed": st.get("start") or t,
                                             "stall_s": _dur(c.opts.get("stall"), 0),
                                             "warmup_s": _dur(c.opts.get("warmup"), 0)})
    val = None
    if rx:
        try:
            ms = [m for ln in lines for m in [re.search(rx, ln)] if m]
        except re.error:
            return
        if ms:
            if ms[-1].groups():
                val = ms[-1].group(1)
            else:
                val = pr["count"] = (pr.get("count") or 0) + len(ms)
    elif lines:
        val = pr["count"] = (pr.get("count") or 0) + len(lines)
    if val is not None and val != pr.get("last"):
        pr["last"] = val
        pr["changed"] = _last_ts(lines, rx, t) if rx else t


def _last_ts(lines, rx, t):
    for ln in reversed(lines):
        if re.search(rx, ln):
            return _ts(ln, t)
    return t


def _series(st: dict, c, rx: str | None, lines: list[str], t: float) -> None:
    if not rx:
        return
    se = st["series"].setdefault(c.name, {"kind": c.kind, "points": [], "n": 0, "peak": None})
    pct = "pct" in c.flags
    try:
        for ln in lines:
            m = re.search(rx, ln)
            if not m:
                continue
            v = _num(m, pct)
            if v is None or not math.isfinite(v):
                continue
            se["points"].append([_ts(ln, t), v])
            se["n"] += 1
            if se["peak"] is None or v > se["peak"]:
                se["peak"] = v
    except re.error:
        return
    se["points"] = se["points"][-MAX_POINTS:]


def _dur(s, default):
    try:
        return contract.dur(s) if s else default
    except contract.ContractError:
        return default


def slope_per_h(points: list) -> float | None:
    pts = points[-SLOPE_POINTS:]
    if len(pts) < 2:
        return None
    ts = [x[0] for x in pts]
    vs = [x[1] for x in pts]
    mt, mv = sum(ts) / len(ts), sum(vs) / len(vs)
    den = sum((x - mt) ** 2 for x in ts)
    if den == 0:   # no timestamps: slope per point
        n = len(vs)
        mi = (n - 1) / 2
        den = sum((i - mi) ** 2 for i in range(n))
        return sum((i - mi) * (v - mv) for i, v in enumerate(vs)) / den
    return sum((x - mt) * (v - mv) for x, v in zip(ts, vs)) / den * 3600


def compact_state(st: dict, t: float | None = None) -> dict:
    t = t or now()
    out = {"state": st.get("state"), "age_h": round((t - (st.get("start") or t)) / 3600, 2)}
    series = {}
    success = None
    for name, se in st.get("series", {}).items():
        pts = se["points"]
        if not pts:
            continue
        last = pts[-1][1]
        sl = slope_per_h(pts)
        series[name] = {"kind": se["kind"], "last": round(last, 4), "n": se["n"],
                        "peak": round(se["peak"], 4), "drop_from_peak": round(se["peak"] - last, 4),
                        "slope_per_h": None if sl is None else round(sl, 4)}
        if se["kind"] == "success" and success is None:
            success = round(last, 4)
    stalled = False
    prog = {}
    t_end = st.get("ended") or t      # an ended job: idle up to its end
    for name, pr in st.get("progress", {}).items():
        idle = t_end - (pr.get("changed") or t_end)
        warm = (t_end - (st.get("start") or t_end)) < pr.get("warmup_s", 0)
        s = bool(pr.get("stall_s")) and not warm and idle > pr["stall_s"]
        prog[name] = {"last": pr.get("last"), "idle_s": round(idle), "stalled": s}
        stalled = stalled or s
    out.update(series=series, progress=prog, stalled=stalled, success=success)
    return out


def compact(p: Project, job: str) -> dict | None:
    st = load(p, job)
    return compact_state(st) if st else None


def points(p: Project, job: str, name: str, n: int = 20) -> list:
    st = load(p, job) or {}
    se = (st.get("series") or {}).get(name) or {}
    return [[round(t), round(v, 4)] for t, v in se.get("points", [])[-n:]]


def metric(tr: dict, name: str):
    """A metric for watches and triggers: success, NAME, NAME.last|slope|peak|n|drop."""
    if tr is None:
        return None
    base, _, field = name.partition(".")
    field = field or "last"
    if base == "success" and "success" not in tr["series"]:
        if field == "last":
            return tr.get("success")
        base = next((k for k, v in tr["series"].items() if v["kind"] == "success"), None)
        if base is None:
            return None
    if base == "age_h":
        return tr.get("age_h")
    se = tr["series"].get(base)
    if se is None:
        pr = tr["progress"].get(base)
        return pr.get("last") if pr and field == "last" else None
    key = {"last": "last", "slope": "slope_per_h", "peak": "peak", "n": "n", "drop": "drop_from_peak"}.get(field)
    return se.get(key) if key else None


def line(job: str, tr: dict) -> str:
    """One line for `tw queue run` / status."""
    bits = [f"{job} {tr['state']} {tr['age_h']}h"]
    for n, s in list(tr["series"].items())[:3]:
        sl = s["slope_per_h"]
        bits.append(f"{n}={s['last']}" + (f" ({sl:+.3g}/h)" if sl is not None else ""))
    if tr["stalled"]:
        bits.append("STALLED")
    return clip(" ".join(bits), 160)

