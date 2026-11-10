"""Local job queue. Every job is a shell script with an output contract.

    tw submit JOB_FILE [--name N]   lint the contract, refuse a job without one
    tw queue run [--once]           run pending jobs; --once exits when empty

A job runs with the project root as cwd, stdout+stderr to
.tierwright/logs/<name>.log. While it runs the verifier checks its contract
every second; a hard failure or the timeout kills it (process group). Its end
is an `end` event, which the dispatcher turns into a verdict without a model
whenever a rule covers it.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import contract, events
from .project import Project, ProjectError, now, read_jsonl, write_json

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class QueueError(ProjectError):
    pass


def submit(p: Project, src: str | Path, name: str | None = None, require_contract: bool = True,
           ext: dict | None = None) -> str:
    p.ensure()
    src = Path(src)
    text = src.read_text()
    contract.parse(text, ext)  # raises ContractError with the line number
    if require_contract and not contract.has_contract(text, ext):
        raise QueueError(f"{src} has no output contract (#@ lines). Every job states what its "
                         "outputs must look like; see `tw contract --help`.")
    name = name or src.stem
    if not NAME_RE.match(name):
        raise QueueError(f"bad job name {name!r}")
    for part in ("pending", "running"):
        if (p.q(part) / f"{name}.job").exists():
            raise QueueError(f"job {name} is already {part}")
    dst = p.q("pending") / f"{name}.job"
    dst.write_text(text)
    events.emit(p, "submit", name)
    return name


def _done_ok(p: Project, name: str) -> bool | None:
    """True / False once `name` (or its one retry) finished; None while waiting."""
    import json
    retry = f"{name}_r1"
    if any((p.q(part) / f"{retry}.job").exists() for part in ("pending", "running")):
        return None
    for n in (retry, name):
        meta = p.q("done") / f"{n}.json"
        if meta.exists():
            m = json.loads(meta.read_text())
            return m.get("rc") == 0 and m.get("contract", {}).get("pass", False)
    return None


def _next(p: Project) -> Path | None:
    for f in sorted(p.q("pending").glob("*.job"), key=lambda f: f.stat().st_mtime):
        v = contract.Verifier(f.read_text(), "", 0)
        deps = [_done_ok(p, a) for a in v.after]
        if any(d is False for d in deps):
            # a dependency failed: this job can never run as asked
            _finish(p, f.stem, f, rc=None, reason="dependency_failed", start=now(),
                    res=[], log=None)
            f.unlink()
            continue
        if all(d is True for d in deps):
            return f
    return None


def _kill(proc: subprocess.Popen, grace: float = 5) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(grace)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    except OSError:
        pass


def _finish(p: Project, name, jobfile, rc, reason, start, res, log) -> dict:
    v = contract.verdict(res)
    meta = {"job": name, "rc": rc, "reason": reason, "start": start, "end": now(),
            "duration_s": round(now() - start, 1), "contract": v, "results": res,
            "log": str(log) if log else None}
    write_json(p.q("done") / f"{name}.json", meta)
    if jobfile and Path(jobfile).exists():
        shutil.copy(jobfile, p.q("done") / f"{name}.job")
    events.emit(p, "end", name, rc=rc, reason=reason, contract_pass=v["pass"],
                duration_s=meta["duration_s"])
    return meta


def run_one(p: Project, jobfile: Path, default_timeout: float = 3600, poll: float = 1.0) -> dict:
    name = jobfile.stem
    running = p.q("running") / jobfile.name
    shutil.move(str(jobfile), running)
    log = p.logs / f"{name}.log"
    start = now()
    text = running.read_text()
    ver = contract.Verifier(text, str(log), start, cwd=str(p.root))
    limit = ver.timeout or default_timeout
    events.emit(p, "start", name)
    with open(log, "w") as out:
        proc = subprocess.Popen(["bash", str(running)], cwd=p.root, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
        reason, res = "exit", []
        while True:
            try:
                proc.wait(poll)
                break
            except subprocess.TimeoutExpired:
                pass
            out.flush()
            res = ver.check(now())
            if contract.verdict(res)["hard_fail"]:
                reason = "contract_stop"
                _kill(proc)
                break
            if now() - start > limit:
                reason = "timeout"
                _kill(proc)
                break
    rc = proc.returncode
    res_end = ver.check(now(), ended=True)
    if reason == "contract_stop":
        # keep the failure that stopped it, plus the end checks
        failed = {r["check"] for r in res if r["status"] == "fail"}
        res_end = [r for r in res if r["check"] in failed] + \
                  [r for r in res_end if r["check"] not in failed]
    meta = _finish(p, name, running, rc, reason, start, res_end, log)
    running.unlink(missing_ok=True)
    return meta


def run(p: Project, once: bool = True, default_timeout: float = 3600, on_end=None,
        idle_sleep: float = 2.0) -> list[dict]:
    """Run pending jobs in order. `on_end(meta)` is the dispatcher hook."""
    p.ensure()
    done = []
    while True:
        f = _next(p)
        if f is None:
            if on_end:
                on_end(None)  # let the dispatcher catch up on dependency-failed ends
            if once:
                return done
            time.sleep(idle_sleep)  # the worker process waits; agents never do
            continue
        meta = run_one(p, f, default_timeout)
        done.append(meta)
        if on_end:
            on_end(meta)


def stop(p: Project, name: str) -> bool:
    f = p.q("pending") / f"{name}.job"
    if f.exists():
        _finish(p, name, f, rc=None, reason="stopped", start=now(), res=[], log=None)
        f.unlink()
        return True
    return False


def counts(p: Project) -> dict:
    c = {part: len(list(p.q(part).glob("*.job"))) for part in ("pending", "running", "done")}
    rdir = p.q("remote")
    if rdir.exists():  # jobs on an ssh queue (remote.py)
        import json
        for f in rdir.glob("*.json"):
            st = json.loads(f.read_text()).get("state")
            if st in ("pending", "running"):
                c[st] += 1
    return c


def last_ends(p: Project, n: int = 5) -> list[dict]:
    return [e for e in read_jsonl(p.events) if e.get("kind") == "end"][-n:]
