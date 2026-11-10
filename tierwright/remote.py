"""Remote queue backend (`queue: {backend: ssh}`): jobs run on another host's
queue, which runs and verifies them; this side submits and reads verdicts.

    tw submit JOB     lint the contract here, then copy the job to the host's
                      pending_dir (scp), or run queue.submit_cmd (the host's own
                      tag/lint/copy script) when one is set
    tw queue run      never runs a job here: pushes local resubmits, then pulls
                      the new ledger lines (byte offset in .tierwright/queue/
                      remote.offset) and turns them into the same events.jsonl
                      records a local run writes, so the dispatcher, the rules
                      and `tw brief` work unchanged

Only jobs submitted from this project are tracked (.tierwright/queue/remote/).
A job ends at its `pass` or `fail` ledger line; `start`, `end` and `stop` lines
record its GPU, rc and a health stop on the way. Each tracked job's ledger
lines go to .tierwright/logs/<job>.log, which is what its brief reads.

No dependency: it shells out to `ssh` and `scp`. Tests replace RUNNER.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import contract, events
from .project import Project, ProjectError, clip, now, write_json

RUNNER = subprocess.run          # tests swap in a fake ssh/scp
TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
READ_CHUNK = 4 * 2**20
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class RemoteError(ProjectError):
    pass


def _rpath(path: str) -> str:
    """Quote a remote path for the remote shell, keeping a leading ~/ expandable."""
    if path == "~":
        return "~"
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def _ts(line: str) -> float | None:
    m = TS_RE.match(line)
    if not m:
        return None
    return float(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")))


class Remote:
    def __init__(self, p: Project, cfg: dict):
        self.p, self.cfg, self.q = p, cfg, cfg["queue"]
        if self.q["backend"] != "ssh":
            raise RemoteError("queue.backend is not ssh")
        self.host = self.q["host"]
        self.dir = p.q("remote")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.offset_f = p.state / "queue" / "remote.offset"
        self.ev = {k: re.compile(v) for k, v in (self.q.get("events") or {}).items() if v}

    # transport --------------------------------------------------------------
    def _opts(self) -> list[str]:
        o = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(self.q['connect_timeout_s'])}"]
        if self.q.get("key"):
            o = ["-i", os.path.expanduser(self.q["key"])] + o
        return o

    def _run(self, argv: list[str], what: str, timeout: float = 120,
             text: bool = True) -> subprocess.CompletedProcess:
        try:
            r = RUNNER(argv, capture_output=True, text=text, timeout=timeout)
        except FileNotFoundError:
            raise RemoteError(f"{argv[0]} is not installed") from None
        except subprocess.TimeoutExpired:
            raise RemoteError(f"cannot reach {self.host}: {what} timed out after {timeout:.0f}s") from None
        if r.returncode == 255:
            e = r.stderr or ""
            err = (e.decode("utf-8", "replace") if isinstance(e, bytes) else e).strip().splitlines()
            raise RemoteError(f"cannot reach {self.host}: {err[-1] if err else 'ssh exit 255'}")
        return r

    def ssh(self, cmd: str, timeout: float = 120, text: bool = True) -> subprocess.CompletedProcess:
        return self._run(["ssh", *self._opts(), self.host, cmd], "ssh", timeout, text)

    def scp(self, local: str, remote: str) -> None:
        # scp resolves a relative remote path against the remote home
        rp = remote[2:] if remote.startswith("~/") else remote
        r = self._run(["scp", "-q", *self._opts(), local, f"{self.host}:{rp}"], "scp")
        if r.returncode:
            raise RemoteError(f"scp to {self.host}:{remote} failed: {clip((r.stderr or '').strip(), 200)}")

    # tracking ---------------------------------------------------------------
    def _rec_path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def tracked(self, open_only: bool = True) -> dict[str, dict]:
        out = {}
        for f in sorted(self.dir.glob("*.json")):
            r = json.loads(f.read_text())
            if not open_only or r.get("state") != "ended":
                out[r["job"]] = r
        return out

    def _save(self, rec: dict) -> None:
        write_json(self._rec_path(rec["job"]), rec)

    def _offset(self) -> int | None:
        return int(self.offset_f.read_text()) if self.offset_f.exists() else None

    def _ledger_size(self) -> int:
        r = self.ssh(f"stat -c %s {_rpath(self.q['ledger'])} 2>/dev/null || echo 0")
        try:
            return int(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RemoteError(f"cannot read the size of {self.q['ledger']} on {self.host}") from None

    # submit -----------------------------------------------------------------
    def submit(self, src: str | Path, name: str | None = None, extra: list[str] | None = None,
               require_contract: bool = True) -> tuple[str, list[str]]:
        """Lint here, copy to the host. Returns (name, lint warnings)."""
        src = Path(src)
        text = src.read_text()
        warn: list[str] = []
        ext = _ext(self.cfg)
        contract.parse(text, ext, warn)
        sent, twarn = translate(text, self.q.get("translate") or {}, self.q.get("header_lines", 3))
        warn += twarn
        if require_contract and not contract.has_contract(text, ext):
            raise RemoteError(f"{src} has no output contract (#@ lines); see `tw contract`.")
        name = (name or src.stem).removesuffix(".job")
        if not NAME_RE.match(name):
            raise RemoteError(f"bad job name {name!r}")
        if name in self.tracked():
            raise RemoteError(f"job {name} is already queued on {self.host} (tw queue run reads its verdict)")
        if self._offset() is None:  # read from here on, so this job's lines are never skipped
            self.offset_f.write_text(str(self._ledger_size()))
        out = self.dir / f"{name}.sent.job"     # what the remote queue receives
        out.write_text(sent)
        if self.q.get("submit_cmd"):
            self._submit_cmd(out, name, extra or [])
        else:
            if extra:
                raise RemoteError("extra submit arguments need queue.submit_cmd")
            dirs = [self.q[k] for k in ("pending_dir", "running_dir", "done_dir") if self.q.get(k)]
            probe = " ".join(f"{_rpath(d)}/{shlex.quote(name)}.job" for d in dirs)
            r = self.ssh(f"mkdir -p {_rpath(self.q['pending_dir'])} && ls {probe} 2>/dev/null; true")
            if r.returncode:
                raise RemoteError(f"{self.q['pending_dir']} on {self.host}: {clip(r.stderr.strip(), 200)}")
            if r.stdout.strip():
                raise RemoteError(f"{name}.job is already pending, running or done on {self.host}: "
                                  "pick another --name")
            self.scp(str(out), f"{self.q['pending_dir'].rstrip('/')}/{name}.job")
        keep = self.dir / f"{name}.job"       # the job as sent, for its brief and a resubmit
        keep.write_text(text)
        self._save({"job": name, "state": "pending", "submitted": now(), "src": str(keep)})
        events.emit(self.p, "submit", name, host=self.host)
        return name, warn

    def _submit_cmd(self, src: Path, name: str, extra: list[str]) -> None:
        q = self.q
        cmd = q["submit_cmd"].format(file=shlex.quote(str(src)), name=shlex.quote(name),
                                     host=shlex.quote(self.host),
                                     key=shlex.quote(os.path.expanduser(q.get("key") or "")))
        if extra:
            cmd += " " + " ".join(shlex.quote(a) for a in extra)
        try:
            r = RUNNER(["bash", "-c", cmd], capture_output=True, text=True, cwd=self.p.root, timeout=300)
        except subprocess.TimeoutExpired:
            raise RemoteError(f"submit_cmd timed out: {cmd}") from None
        if r.returncode:
            err = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
            raise RemoteError(f"submit_cmd failed (rc={r.returncode}): {err[-1] if err else cmd}")

    def adopt(self, name: str) -> dict | None:
        """Track a job queued on the host by other means (its own submit script),
        so `tw watch` and trends cover it. Reads its file from running or pending."""
        name = name.removesuffix(".job")
        if not NAME_RE.match(name):
            raise RemoteError(f"bad job name {name!r}")
        if name in self.tracked(open_only=False):
            return self.tracked(open_only=False)[name]
        for state, d in (("running", self.q.get("running_dir")), ("pending", self.q.get("pending_dir"))):
            if not d:
                continue
            f = f"{_rpath(d)}/{shlex.quote(name)}.job"
            r = self.ssh(f"test -f {f} && stat -c %Y {f} && cat {f}")
            if r.returncode == 0 and r.stdout:
                mtime, _, text = r.stdout.partition("\n")
                if self._offset() is None:
                    self.offset_f.write_text(str(self._ledger_size()))
                keep = self.dir / f"{name}.job"
                keep.write_text(text)
                rec = {"job": name, "state": state, "submitted": now(), "src": str(keep), "adopted": True}
                if state == "running":
                    rec["start"] = float(_int(mtime.strip(), now()))
                self._backfill(name)
                self._save(rec)
                events.emit(self.p, "adopt", name, host=self.host, state=state)
                return rec
        return None

    def _backfill(self, name: str, n: int = 5000) -> None:
        """An adopted job's ledger lines so far (by name or gq tag) into its local log."""
        tag = "gq-" + name.replace("_", "-")
        rx = f"(^|[^A-Za-z0-9_-])({re.escape(name)}|{re.escape(tag)})([^A-Za-z0-9_-]|$)"
        r = self.ssh(f"grep -a -E {shlex.quote(rx)} {_rpath(self.q['ledger'])} | tail -n {n}; true")
        if r.stdout:
            with open(self.p.logs / f"{name}.log", "a") as f:
                f.write(r.stdout if r.stdout.endswith("\n") else r.stdout + "\n")

    def stop(self, name: str) -> bool:
        if name not in self.tracked():
            return False
        f = f"{_rpath(self.q['pending_dir'])}/{shlex.quote(name)}.job"
        r = self.ssh(f"test -f {f} && rm -f {f} && echo removed; true")
        if "removed" not in r.stdout:
            return False
        self._end(name, rc=None, reason="stopped", detail="removed from pending", t=now())
        return True

    # pull -------------------------------------------------------------------
    def push_local(self) -> list[str]:
        """Local pending jobs (the dispatcher's resubmits) go to the host."""
        sent = []
        for f in sorted(self.p.q("pending").glob("*.job")):
            name, _ = self.submit(f, f.stem)
            f.unlink()
            sent.append(name)
        return sent

    def pull(self) -> list[dict]:
        """Read new ledger lines; returns the events emitted for tracked jobs."""
        off = self._offset()
        if off is None:
            off = self._ledger_size()
            self.offset_f.write_text(str(off))
            return []
        out = []
        while True:
            size = self._ledger_size()
            if size < off:      # rotated or truncated: start over
                off = 0
            if size == off:
                break
            n = min(READ_CHUNK, size - off)
            r = self.ssh(f"tail -c +{off + 1} {_rpath(self.q['ledger'])} | head -c {n}", timeout=300,
                         text=False)
            if r.returncode:
                raise RemoteError(f"cannot read {self.q['ledger']} on {self.host}: "
                                  f"{clip(r.stderr.decode('utf-8', 'replace').strip(), 200)}")
            data = r.stdout
            cut = data.rfind(b"\n")
            if cut < 0:
                break           # a partial last line: read it next time
            chunk = data[:cut + 1]
            out += self._apply(chunk.decode("utf-8", "replace").splitlines())
            off += len(chunk)
            self.offset_f.write_text(str(off))
            if n < READ_CHUNK:
                break
        return out

    def _match(self, line: str, jobs: dict) -> tuple[str, re.Match] | None:
        for kind in ("start", "end", "stop", "pass", "fail", "skip"):
            rx = self.ev.get(kind)
            m = rx.search(line) if rx else None
            if m and m.group("job").removesuffix(".job") in jobs:
                return kind, m
        return None

    def _apply(self, lines: list[str]) -> list[dict]:
        jobs = self.tracked()
        if not jobs:
            return []
        out = []
        # the job's name, or the gq-style tag `gq-<name with - for _>`
        pats = {j: re.compile(rf"(?<![\w-])(?:{re.escape(j)}|gq-{re.escape(j.replace('_', '-'))})(?![\w-])")
                for j in jobs}
        for line in lines:
            hits = [j for j, rx in pats.items() if rx.search(line)]
            if not hits:
                continue
            for j in hits:
                with open(self.p.logs / f"{j}.log", "a") as f:
                    f.write(line + "\n")
            m = self._match(line, jobs)
            if not m:
                continue
            kind, mm = m
            job = mm.group("job").removesuffix(".job")
            rec = jobs[job]
            t = _ts(line) or now()
            gd = mm.groupdict()
            if kind == "start":
                rec.update(state="running", start=t, gpu=gd.get("gpu"))
                self._save(rec)
                out.append(events.emit(self.p, "start", job, host=self.host, gpu=gd.get("gpu")))
            elif kind == "end":
                rec["rc"] = _int(gd.get("rc"), None)
                self._save(rec)
            elif kind == "stop":
                rec["stopped"] = clip(gd.get("detail") or line, 300)
                self._save(rec)
            else:
                rc = _int(gd.get("rc"), rec.get("rc"))
                reason = "exit"
                if kind == "skip":
                    reason = "preempted"
                elif kind == "fail" and rec.get("stopped"):
                    reason = "contract_stop"
                out.append(self._end(job, rc, reason, gd.get("detail") or "", t, passed=kind == "pass"))
                jobs.pop(job)
                pats.pop(job)
        return out

    def _end(self, job: str, rc, reason: str, detail: str, t: float, passed: bool = False) -> dict:
        rec = json.loads(self._rec_path(job).read_text())
        start = rec.get("start") or rec.get("submitted") or t
        failed = [] if passed or reason == "preempted" else [
            {"check": part.split(":", 1)[0].strip(), "kind": "remote", "status": "fail",
             "evidence": clip(part.split(":", 1)[-1].strip(), 300), "hard": True}
            for part in re.split(r";\s+", detail) if part.strip()]
        if not passed and not failed and reason == "exit":
            failed = [{"check": "remote", "kind": "remote", "status": "fail",
                       "evidence": "contract failed", "hard": True}]
        v = {"pass": passed, "hard_fail": bool(failed), "failed": failed, "checked": len(failed)}
        log = self.p.logs / f"{job}.log"
        meta = {"job": job, "rc": rc, "reason": reason, "start": start, "end": t,
                "duration_s": round(max(0.0, t - start), 1), "contract": v, "results": failed,
                "log": str(log) if log.exists() else None, "host": self.host, "gpu": rec.get("gpu")}
        write_json(self.p.q("done") / f"{job}.json", meta)
        src = rec.get("src")
        if src and Path(src).exists() and not (self.p.q("done") / f"{job}.job").exists():
            (self.p.q("done") / f"{job}.job").write_text(Path(src).read_text())
        rec.update(state="ended", rc=rc, reason=reason, passed=passed, end=t)
        self._save(rec)
        return events.emit(self.p, "end", job, rc=rc, reason=reason, contract_pass=passed,
                           duration_s=meta["duration_s"], host=self.host)


def _int(s, default):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


HEADER_RE = re.compile(r"^# ?[A-Z][A-Z0-9_]*=\S")


def translate(text: str, tr: dict, header_lines: int = 3) -> tuple[str, list[str]]:
    """Tierwright-only `#@ timeout` / `#@ after` -> the remote queue's header lines.

    Returns (job text to send, warnings). A directive whose template is null is
    stripped with a warning. Conflicts (two timeouts, a timeout next to the job's
    own header, more than one `after`, headers past `header_lines`) raise."""
    checks = {c.line: c for c in contract.parse(text, {"unknown": "warn"}, [])
              if c.kind in ("timeout", "after")}
    if not checks:
        return text, []
    lines = text.splitlines()
    warn, heads = [], []
    kinds = [c.kind for c in checks.values()]
    for k in ("timeout", "after"):
        if kinds.count(k) > 1:
            raise RemoteError(f"{kinds.count(k)} `#@ {k}` lines: the remote queue takes one")
    for c in checks.values():
        tpl = tr.get(c.kind)
        if tpl is None:
            warn.append(f"line {c.line}: #@ {c.kind} has no equivalent on the remote queue: stripped")
            continue
        if c.kind == "timeout":
            s = contract.dur(c.arg)
            h = tpl.format(hours=f"{s / 3600:.4g}", minutes=f"{s / 60:.4g}", seconds=f"{s:.0f}")
        else:
            if not NAME_RE.match(c.arg):
                raise RemoteError(f"line {c.line}: #@ after {c.arg!r} is not a job name")
            h = tpl.format(job=c.arg)
        key = h.split("=", 1)[0] + "=" if HEADER_RE.match(h) else None
        if key and any(l.startswith(key) for l in lines[:header_lines + 1]):
            raise RemoteError(f"line {c.line}: #@ {c.kind} and the job's own `{key}` header conflict")
        heads.append(h)
    body = [l for i, l in enumerate(lines, 1) if i not in checks]
    top = [body.pop(0)] if body and body[0].startswith("#!") else []
    # keep the job's own leading headers (# GPUS=2) inside the lines the queue reads
    while body and HEADER_RE.match(body[0]):
        top.append(body.pop(0))
    top += heads
    out = top + body
    want = heads + [l for l in lines[:header_lines] if HEADER_RE.match(l)]
    lost = [l for l in want if l not in out[:header_lines]]
    if lost:
        raise RemoteError(f"the remote queue reads headers from the first {header_lines} lines; "
                          f"{', '.join(lost)} would fall outside them: move or drop one")
    return "\n".join(out) + "\n", warn


def _ext(cfg: dict) -> dict:
    from . import config
    return config.contract_ext(cfg)


def run(p: Project, cfg: dict, once: bool = True, on_end=None, on_tick=None) -> list[dict]:
    """`tw queue run` for an ssh queue: push, pull, read running jobs' trends,
    dispatch, check watches, tune. Never runs a job here."""
    from . import trend, tune, watch
    r = Remote(p, cfg)
    ended = []
    while True:
        r.push_local()
        evs = r.pull()
        trends = trend.ingest(p, cfg, r)
        ends = [e for e in evs if e["kind"] == "end"]
        ended += ends
        if on_end:
            for e in ends:
                on_end(json.loads((p.q("done") / f"{e['job']}.json").read_text()))
            if not ends:
                on_end(None)
        fired = watch.check(p, cfg)
        tuned = tune.scan(p, cfg, r)
        if any(p.q("pending").glob("*.job")):
            r.push_local()      # a resubmit or tuned copy goes out now, not next run
        if on_tick:
            on_tick(trends, fired, tuned)
        if once:
            return ended
        time.sleep(cfg["queue"]["poll_s"])  # the worker waits; agents never do
