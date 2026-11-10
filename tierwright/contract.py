"""Output contracts: every job states what its outputs must look like, and the
verifier checks them while it runs and when it ends.

Monitoring verifies OUTPUTS, not liveness: a process that is alive but
producing nothing, or producing garbage (a flat loss, a missing file, a result
without its keys, a pass rate under its floor) fails its contract.

A contract is `#@` comment lines anywhere in a job file. One check per line,
shlex-split; quote every regex. `${VAR}` / `${VAR:-default}` expand from the
environment.

  #@ log PATH                       default source for the checks below (default: job stdout)
  #@ after JOB                      do not start before JOB finished ok (queue dependency)
  #@ timeout DUR                    whole job must finish within DUR
  #@ phase NAME until=RE max=DUR    RE must appear within DUR of the start
  #@ progress NAME [re=RE] stall=DUR [warmup=DUR]
        the counter (RE group 1, else the number of RE matches, else the
        source size) must change at least every `stall` once `warmup` passed
  #@ forbid NAME re=RE              any match fails (a crash signature)
  #@ value NAME re=RE [finite] [min=X] [max=X]
        every captured number (group 1) must be finite / within bounds
  #@ artifact PATH [min_size=B]     at end: file/glob exists, newer than the job's start
  #@ result PATH keys=a,b.c         at end: JSON file with these (dotted) keys
  #@ success NAME re=RE min=F       at end: last k/n (two groups) or fraction >= F

Every check takes `in=PATH` to read another file. Durations: 90s, 20m, 2h,
bare number = seconds. A failed hard check stops the job (enforce mode, the
default); `#@ mode warn` only records.

Another verifier's contract dialect (a remote queue that enforces its own
`#@` lines) lints through `contract:` in tierwright.yaml: `kinds`, `opts`
({kind: [option, ...]}) and `flags` add accepted words, and `unknown: warn`
turns any other unknown word or missing option into a warning instead of an
error (`auto`, the default: warn with an ssh queue, error locally). Syntax
errors (quoting, regexes, durations) are always errors. Checks the local
verifier does not know are skipped by it.
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

KINDS = {"log", "after", "timeout", "mode", "phase", "progress", "forbid", "value",
         "artifact", "result", "success"}
OPTS = {
    "phase": {"until", "max", "in"},
    "progress": {"re", "stall", "warmup", "in"},
    "forbid": {"re", "in"},
    "value": {"re", "min", "max", "in"},
    "artifact": {"min_size"},
    "result": {"keys"},
    "success": {"re", "min", "in"},
}
FLAGS = {"finite", "warn"}
# required options; each entry is a set of alternatives. The alternatives after the
# first are a foreign verifier's forms (ref= floor, max= ceiling, tag= / key= sources): they count
# only once `contract.opts` declares them.
NEEDS = {"phase": [["until"], ["max"]], "progress": [["stall"]], "forbid": [["re"]],
         "value": [["re", "tag", "key"]], "success": [["re"], ["min", "ref", "max"]], "result": [["keys"]]}
LOCAL_NEEDS = {"value": ["re"], "success": ["re", "min"]}
END_ONLY = {"artifact", "result", "success"}
READ_MAX = 8 * 2**20


class ContractError(ValueError):
    pass


def dur(s: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smh]?)", str(s).strip())
    if not m:
        raise ContractError(f"bad duration {s!r} (e.g. 90s, 20m, 2h)")
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "": 1}[m.group(2)]


def expand(s: str, env: dict | None = None) -> str:
    env = os.environ if env is None else env

    def sub(m):
        name, default = m.group(1) or m.group(3), m.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        raise ContractError(f"${name} is not set")

    s = re.sub(r"\$\{(\w+)(?::-([^}]*))?\}|\$(\w+)", sub, s)
    return os.path.expanduser(s)


@dataclass
class Check:
    kind: str
    name: str
    arg: str = ""
    opts: dict = field(default_factory=dict)
    flags: set = field(default_factory=set)
    line: int = 0


def _ext(ext: dict | None) -> tuple[set, dict, set, bool]:
    ext = ext or {}
    kinds = KINDS | set(ext.get("kinds") or [])
    opts = {k: set(v) for k, v in OPTS.items()}
    for k, v in (ext.get("opts") or {}).items():
        opts.setdefault(k, set()).update(v or [])
    return kinds, opts, FLAGS | set(ext.get("flags") or []), ext.get("unknown") == "warn"


def parse(text: str, ext: dict | None = None, warnings: list | None = None) -> list[Check]:
    """`ext`: the `contract:` config (extra kinds / opts / flags, unknown: warn).
    Under unknown: warn, problems a foreign verifier may accept go to `warnings`."""
    kinds_ok, opts_ok, flags_ok, lenient = _ext(ext)
    warnings = [] if warnings is None else warnings

    def soft(msg):
        if not lenient:
            raise ContractError(msg)
        warnings.append(msg)

    checks = []
    for i, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s.startswith("#@"):
            continue
        try:
            toks = shlex.split(s[2:])
        except ValueError as e:
            raise ContractError(f"line {i}: {e}") from None
        if not toks:
            continue
        kind, rest = toks[0], toks[1:]
        if kind not in kinds_ok:
            soft(f"line {i}: unknown check {kind!r} (one of {sorted(kinds_ok)})")
        opts, flags, pos = {}, set(), []
        for t in (rest if kind != "mode" else []):
            if "=" in t and not t.startswith("="):
                k, v = t.split("=", 1)
                if k not in opts_ok.get(kind, set()) and kind in kinds_ok:
                    soft(f"line {i}: {kind} takes no option {k!r}")
                opts[k] = v
            elif t in flags_ok or (kind in KINDS and pos):
                if t not in flags_ok:
                    soft(f"line {i}: {kind} has unknown flag {t!r}")
                flags.add(t)
            else:
                pos.append(t)
        if kind not in KINDS:
            # a check only a foreign verifier knows: syntax-check its regexes, keep it
            for k in ("re", "until", "after"):
                if k in opts:
                    try:
                        re.compile(opts[k])
                    except re.error as e:
                        raise ContractError(f"line {i}: bad regex {opts[k]!r}: {e}") from None
            checks.append(Check(kind, f"{kind}:{' '.join(pos)}", " ".join(pos), opts, flags, i))
            continue
        if kind == "mode":
            pos = rest
        if kind in ("log", "after", "timeout", "mode", "artifact", "result"):
            if len(pos) != 1:
                raise ContractError(f"line {i}: {kind} takes exactly one argument")
            c = Check(kind, f"{kind}:{pos[0]}", pos[0], opts, flags, i)
        else:
            if len(pos) != 1:
                raise ContractError(f"line {i}: {kind} needs a NAME")
            c = Check(kind, pos[0], "", opts, flags, i)
        for need in NEEDS.get(kind, []):
            # an alternative counts only when it is accepted (built in, or declared in config)
            if not any(k in opts and k in opts_ok.get(kind, set()) for k in need):
                soft(f"line {i}: {kind} needs {'= or '.join(need)}=")
        for k in ("re", "until"):
            if k in opts:
                try:
                    re.compile(opts[k])
                except re.error as e:
                    raise ContractError(f"line {i}: bad regex {opts[k]!r}: {e}") from None
        for k in ("max", "stall", "warmup"):
            if k in opts and kind != "value":
                dur(opts[k])
        if kind == "timeout":
            dur(c.arg)
        if kind == "mode" and c.arg not in ("enforce", "warn"):
            raise ContractError(f"line {i}: mode is enforce or warn")
        checks.append(c)
    return checks


def has_contract(text: str, ext: dict | None = None) -> bool:
    return any(c.kind not in ("after", "mode", "log") for c in parse(text, ext))


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - READ_MAX))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _dig(d, key: str):
    for part in key.split("."):
        if isinstance(d, dict) and part in d:
            d = d[part]
        else:
            raise KeyError(key)
    return d


class Verifier:
    """Evaluates one job's contract. Call check() while it runs and at end."""

    def __init__(self, text: str, stdout_path: str, start: float, env: dict | None = None,
                 cwd: str | None = None, ext: dict | None = None):
        # checks in a foreign form (no re= / min= of their own) are the remote verifier's
        self.checks = [c for c in parse(text, ext) if c.kind in KINDS
                       and all(k in c.opts for k in LOCAL_NEEDS.get(c.kind, []))]
        self.start = start
        self.cwd = cwd or os.getcwd()
        self.env = dict(os.environ if env is None else env)
        self.stdout = stdout_path
        logs = [c.arg for c in self.checks if c.kind == "log"]
        self.default_src = self._path(logs[0]) if logs else stdout_path
        modes = [c.arg for c in self.checks if c.kind == "mode"]
        self.mode = modes[-1] if modes else "enforce"
        self._prog: dict[str, tuple[object, float]] = {}

    def _path(self, s: str) -> str:
        s = expand(s, self.env)
        return s if os.path.isabs(s) else os.path.join(self.cwd, s)

    def _src(self, c: Check) -> str:
        return self._path(c.opts["in"]) if "in" in c.opts else self.default_src

    @property
    def timeout(self) -> float | None:
        t = [dur(c.arg) for c in self.checks if c.kind == "timeout"]
        return min(t) if t else None

    @property
    def after(self) -> list[str]:
        return [c.arg for c in self.checks if c.kind == "after"]

    def check(self, t: float, ended: bool = False) -> list[dict]:
        res = []
        for c in self.checks:
            if c.kind in ("log", "after", "mode"):
                continue
            if c.kind in END_ONLY and not ended:
                res.append(self._r(c, "pending", "checked at end"))
                continue
            try:
                res.append(getattr(self, "_" + c.kind)(c, t, ended))
            except ContractError as e:
                res.append(self._r(c, "pending" if not ended else "fail", str(e)))
        return res

    def _r(self, c: Check, status: str, evidence: str) -> dict:
        return {"check": c.name, "kind": c.kind, "status": status, "evidence": evidence[:300],
                "hard": "warn" not in c.flags and self.mode == "enforce"}

    # checks ---------------------------------------------------------------
    def _timeout(self, c, t, ended):
        lim = dur(c.arg)
        el = t - self.start
        return self._r(c, "fail" if el > lim else "ok", f"{el:.0f}s of {lim:.0f}s")

    def _phase(self, c, t, ended):
        text = _read(self._src(c))
        if re.search(c.opts["until"], text, re.M):
            return self._r(c, "ok", f"saw {c.opts['until']!r}")
        if t - self.start > dur(c.opts["max"]) or ended:
            return self._r(c, "fail", f"no {c.opts['until']!r} within {c.opts['max']}")
        return self._r(c, "pending", "waiting")

    def _progress(self, c, t, ended):
        src = self._src(c)
        if "re" in c.opts:
            ms = list(re.finditer(c.opts["re"], _read(src), re.M))
            if ms and ms[-1].groups():
                val = ms[-1].group(1)
            else:
                val = len(ms)
        else:
            val = os.path.getsize(src) if os.path.exists(src) else 0
        last = self._prog.get(c.name)
        if last is None or last[0] != val:
            self._prog[c.name] = (val, t)
            return self._r(c, "ok", f"counter {val}")
        if ended:
            return self._r(c, "ok", f"counter {val} at end")
        warm = dur(c.opts.get("warmup", "0"))
        if t - self.start < warm:
            return self._r(c, "pending", "warmup")
        idle = t - last[1]
        if idle > dur(c.opts["stall"]):
            return self._r(c, "fail", f"counter stuck at {val} for {idle:.0f}s")
        return self._r(c, "ok", f"counter {val}")

    def _forbid(self, c, t, ended):
        m = re.search(c.opts["re"], _read(self._src(c)), re.M)
        if m:
            line = _read(self._src(c))[max(0, m.start() - 80): m.end() + 80]
            return self._r(c, "fail", f"forbidden {c.opts['re']!r}: {' '.join(line.split())}")
        return self._r(c, "ok", "absent")

    def _value(self, c, t, ended):
        vals = []
        for m in re.finditer(c.opts["re"], _read(self._src(c)), re.M):
            try:
                vals.append(float(m.group(1)))
            except (IndexError, ValueError, TypeError):
                vals.append(float("nan"))
        if not vals:
            return self._r(c, "fail" if ended else "pending", "no value seen")
        bad = [v for v in vals if ("finite" in c.flags and not math.isfinite(v))
               or ("min" in c.opts and v < float(c.opts["min"]))
               or ("max" in c.opts and v > float(c.opts["max"]))]
        if bad:
            return self._r(c, "fail", f"{len(bad)} bad of {len(vals)}, e.g. {bad[0]}")
        return self._r(c, "ok", f"{len(vals)} values, last {vals[-1]}")

    def _artifact(self, c, t, ended):
        pat = self._path(c.arg)
        hits = [h for h in glob.glob(pat) if os.path.getmtime(h) >= self.start - 1]
        if not hits:
            return self._r(c, "fail", f"no {c.arg} written by this job")
        mn = int(c.opts.get("min_size", 1))
        small = [h for h in hits if os.path.isfile(h) and os.path.getsize(h) < mn]
        if small:
            return self._r(c, "fail", f"{os.path.basename(small[0])} under {mn} bytes")
        return self._r(c, "ok", f"{len(hits)} file(s)")

    def _result(self, c, t, ended):
        path = self._path(c.arg)
        if not os.path.exists(path) or os.path.getmtime(path) < self.start - 1:
            return self._r(c, "fail", f"no {c.arg} written by this job")
        try:
            d = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as e:
            return self._r(c, "fail", f"{c.arg} is not JSON: {e}")
        missing = []
        for k in c.opts["keys"].split(","):
            try:
                _dig(d, k)
            except KeyError:
                missing.append(k)
        if missing:
            return self._r(c, "fail", f"missing keys {missing}")
        return self._r(c, "ok", "all keys present")

    def _success(self, c, t, ended):
        ms = list(re.finditer(c.opts["re"], _read(self._src(c)), re.M))
        if not ms:
            return self._r(c, "fail", "no success line")
        g = ms[-1].groups()
        try:
            v = float(g[0]) / float(g[1]) if len(g) >= 2 else float(g[0])
        except (ValueError, ZeroDivisionError, IndexError):
            return self._r(c, "fail", f"cannot read a rate from {ms[-1].group(0)!r}")
        floor = float(c.opts["min"])
        return self._r(c, "ok" if v >= floor else "fail", f"{v:.3f} vs floor {floor}")


def verdict(res: list[dict]) -> dict:
    fails = [r for r in res if r["status"] == "fail"]
    hard = [r for r in fails if r["hard"]]
    return {"pass": not fails, "hard_fail": bool(hard), "failed": fails,
            "checked": len(res)}
