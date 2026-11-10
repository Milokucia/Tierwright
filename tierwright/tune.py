"""Live-run improvement without an agent: the `tune` role (T2, api).

When a running or ended job's trend matches a trigger, code builds a small
JSON brief and calls `tune` once:

    {job, contract, trend, points (last N of the watched metric), knobs (the
     job's whitelist with current values), do_not_repeat, budget}

The answer ({action: change|none, changes: [{knob, value}], why}) is
schema-checked by the router, then by `check()` here:
    - every knob is on the job's whitelist, once
    - every value parses and sits in its range / choices, and differs from now
    - no value names a protected path
    - the change set is not one already tried on this job (do-not-repeat)
    - compute-scaling knobs (`scales: true`) grow the run by at most max_scale
A rejected answer goes back with the errors, then up a tier (the router);
after the last tier the case goes to owner-queue.md. A valid change is applied
as a resubmit, <job>_tN, with the env vars / flags rewritten, and recorded in
the ledger (kind tune). When the tuned job ends, its verdict is recorded on
that change: a failed change joins the do-not-repeat list.

Caps: tune.max_per_job calls per job family, tune.max_per_day calls a day.

    tune:
      enabled: true
      max_per_job: 2
      max_per_day: 6
      points: 20
      max_tier: T3          # never escalate past this
      triggers: {stall: true, plateau: {window: 5, eps: 0.01}, decay: {drop: 0.1}}
      jobs:
        - match: "imitate_*"     # fnmatch on the job name
          metric: success        # the series the triggers watch
          target: 0.9            # plateau counts only below this
          max_scale: 2.0
          knobs:
            PATIENCE: {type: int, min: 1, max: 6, default: 3}
            DART_NOISE: {type: choice, choices: ["0.2 0.4", "0.1 0.2"], default: "0.2 0.4"}
            REC_ENVS: {type: int, min: 256, max: 4096, default: 1024, scales: true}
            lr: {flag: --lr, type: float, min: 1e-5, max: 1e-2}
"""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
from pathlib import Path

from . import events, ledger, router, trend
from .project import Project, clip, now, owner_note, write_json

ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FLAG_RE = re.compile(r"^--?[A-Za-z][\w-]*$")
TYPES = ("int", "float", "choice", "str")
TRIGGERS = ("stall", "plateau", "decay")


# ── config ──────────────────────────────────────────────────────────────────
def validate_cfg(t: dict) -> list[str]:
    errs = []
    if not isinstance(t, dict):
        return ["tune must be a mapping"]
    for k in ("max_per_job", "max_per_day", "points"):
        if not isinstance(t.get(k), int) or t[k] < 0:
            errs.append(f"tune.{k} must be an integer >= 0")
    if t.get("max_tier") not in ("T1", "T2", "T3"):
        errs.append("tune.max_tier must be T1, T2 or T3")
    tr = t.get("triggers") or {}
    for k in tr:
        if k not in TRIGGERS:
            errs.append(f"tune.triggers.{k}: unknown trigger (one of {TRIGGERS})")
    for i, j in enumerate(t.get("jobs") or []):
        w = f"tune.jobs[{i}]"
        if not isinstance(j, dict) or not j.get("match"):
            errs.append(f"{w} needs match: GLOB")
            continue
        knobs = j.get("knobs") or {}
        if not knobs:
            errs.append(f"{w} needs at least one knob")
        if not isinstance(j.get("max_scale", 2.0), (int, float)) or j.get("max_scale", 2.0) < 1:
            errs.append(f"{w}.max_scale must be a number >= 1")
        for name, k in knobs.items():
            kw = f"{w}.knobs.{name}"
            if not isinstance(k, dict):
                errs.append(f"{kw} must be a mapping")
                continue
            if k.get("flag"):
                if not FLAG_RE.match(str(k["flag"])):
                    errs.append(f"{kw}.flag must look like --name")
            elif not ENV_RE.match(name):
                errs.append(f"{kw}: an env var name, or set flag: --name")
            ty = k.get("type", "str")
            if ty not in TYPES:
                errs.append(f"{kw}.type must be one of {TYPES}")
            if ty == "choice" and not k.get("choices"):
                errs.append(f"{kw} type choice needs choices")
            if ty in ("int", "float"):
                for b in ("min", "max"):
                    if b in k and not isinstance(k[b], (int, float)):
                        errs.append(f"{kw}.{b} must be a number")
                if "min" in k and "max" in k and isinstance(k["min"], (int, float)) \
                        and isinstance(k["max"], (int, float)) and k["min"] > k["max"]:
                    errs.append(f"{kw}: min > max")
            if k.get("scales") and ty not in ("int", "float"):
                errs.append(f"{kw}: only int / float knobs can scale compute")
    return errs


def job_cfg(cfg: dict, job: str) -> dict | None:
    t = cfg.get("tune") or {}
    if not t.get("enabled"):
        return None
    base = family(job)
    for j in t.get("jobs") or []:
        if fnmatch.fnmatch(job, j["match"]) or fnmatch.fnmatch(base, j["match"]):
            return j
    return None


def family(job: str) -> str:
    """imitate_x_t2_r1 -> imitate_x: tuned copies and retries share one budget."""
    return re.sub(r"(_t\d+|_r1)+$", "", job)


# ── knobs in a job file ─────────────────────────────────────────────────────
def _assign_re(name: str) -> re.Pattern:
    # NAME=value as a shell assignment (not inside ${NAME:-...}, not part of a longer word)
    return re.compile(rf"(?<![\w${{:-]){re.escape(name)}=(\"[^\"]*\"|'[^']*'|[^\s;&|)]*)")


def _flag_re(flag: str) -> re.Pattern:
    return re.compile(rf"(?<![\w-]){re.escape(flag)}(=|\s+)(\"[^\"]*\"|'[^']*'|[^\s;&|)]+)")


def _unq(s: str) -> str:
    return s[1:-1] if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"" else s


def current(text: str, name: str, k: dict) -> str | None:
    if k.get("flag"):
        m = _flag_re(k["flag"]).search(text)
        return _unq(m.group(2)) if m else None
    m = _assign_re(name).search(text)
    if m:
        return _unq(m.group(1))
    m = re.search(rf"\$\{{{re.escape(name)}:-([^}}]*)\}}", text)
    if m:
        return m.group(1)
    return None if k.get("default") is None else str(k["default"])


def apply_knobs(text: str, changes: dict[str, str], knobs: dict) -> str:
    """Rewrite each knob's assignment or flag; an env knob not assigned anywhere
    gets an `export` after the job's leading comment/header block."""
    exports = []
    for name, val in changes.items():
        k = knobs[name]
        q = shlex.quote(val)
        if k.get("flag"):
            rx = _flag_re(k["flag"])
            text = rx.sub(lambda m: f"{k['flag']}{m.group(1)}{q}", text)
            continue
        rx = _assign_re(name)
        if rx.search(text):
            text = rx.sub(lambda m: f"{name}={q}", text)
        else:
            exports.append(f"export {name}={q}")
    if exports:
        lines = text.splitlines()
        i = 0
        while i < len(lines) and (lines[i].startswith("#") or not lines[i].strip()):
            i += 1
        lines[i:i] = exports
        text = "\n".join(lines) + "\n"
    return text


# ── triggers ────────────────────────────────────────────────────────────────
def triggers(cfg: dict, jc: dict, tr: dict | None) -> list[str]:
    if not tr:
        return []
    t = (cfg.get("tune") or {}).get("triggers") or {}
    out = []
    if t.get("stall", True) and tr.get("stalled"):
        out.append("stall")
    se = tr["series"].get(jc.get("metric", "success"))
    if se is None and jc.get("metric", "success") == "success":
        se = next((v for v in tr["series"].values() if v["kind"] == "success"), None)
    if se is None:
        return out
    return out + _series_triggers(t, jc, se, tr.get("_points") or [])


def _series_triggers(t: dict, jc: dict, se: dict, pts: list) -> list[str]:
    out = []
    target = jc.get("target")
    pl = t.get("plateau", {"window": 5, "eps": 0.01})
    if pl:
        w = int(pl.get("window", 5))
        vals = [v for _, v in pts[-w:]]
        if len(vals) >= w and max(vals) - min(vals) <= float(pl.get("eps", 0.01)) \
                and (target is None or vals[-1] < float(target)):
            out.append("plateau")
    dc = t.get("decay", {"drop": 0.1})
    if dc:
        n_after = 0
        if pts:
            peak_i = max(range(len(pts)), key=lambda i: pts[i][1])
            n_after = len(pts) - 1 - peak_i
        if se["drop_from_peak"] >= float(dc.get("drop", 0.1)) and n_after >= int(dc.get("min_points", 2)):
            out.append("decay")
    return out


# ── the call ────────────────────────────────────────────────────────────────
def _tries(p: Project, base: str) -> list[dict]:
    return [r for r in ledger.entries(p, kind="tune") if r.get("parent") == base]


def _today(p: Project) -> int:
    day = router._day_start()
    return sum(1 for r in ledger.entries(p, kind="tune") if r.get("ts", 0) >= day and r.get("called"))


def change_key(changes: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in sorted(changes.items()))


def _num(ty: str, v: str):
    return int(v) if ty == "int" else float(v)


def check(ans: dict, jc: dict, cur: dict, tried: set[str], protected: list[str]) -> list[str]:
    """Errors in a tune answer; [] means it may be applied."""
    errs = []
    act, ch = ans.get("action"), ans.get("changes") or []
    if act == "none":
        return ["action none must not list changes"] if ch else []
    if not ch:
        return ["action change needs at least one change"]
    knobs = jc.get("knobs") or {}
    seen, new = set(), {}
    for c in ch:
        name, val = c.get("knob"), str(c.get("value", ""))
        if name not in knobs:
            errs.append(f"knob {name!r} is not on this job's whitelist {sorted(knobs)}")
            continue
        if name in seen:
            errs.append(f"knob {name} listed twice")
            continue
        seen.add(name)
        k = knobs[name]
        ty = k.get("type", "str")
        if ty in ("int", "float"):
            try:
                x = _num(ty, val)
            except ValueError:
                errs.append(f"{name}={val!r} is not an {ty}")
                continue
            if "min" in k and x < k["min"] or "max" in k and x > k["max"]:
                errs.append(f"{name}={val} out of range [{k.get('min', '-inf')}, {k.get('max', 'inf')}]")
                continue
        elif ty == "choice" and val not in [str(x) for x in k["choices"]]:
            errs.append(f"{name}={val!r} not one of {k['choices']}")
            continue
        if not val.strip() or "\n" in val:
            errs.append(f"{name}: empty or multi-line value")
            continue
        if cur.get(name) is not None and str(cur[name]) == val:
            errs.append(f"{name}={val} is the current value: no change")
            continue
        for g in protected:
            if fnmatch.fnmatch(val, g) or fnmatch.fnmatch(val.lstrip("./"), g):
                errs.append(f"{name}={val} names a protected path ({g})")
                break
        else:
            new[name] = val
    if errs:
        return errs
    key = change_key(new)
    if key.lower() in tried:
        errs.append(f"{key} was already tried on this job (do-not-repeat)")
    scale = 1.0
    for name, val in new.items():
        k = knobs[name]
        if k.get("scales"):
            old = cur.get(name)
            try:
                o, n = _num(k.get("type"), str(old)), _num(k.get("type"), val)
                if o > 0:
                    scale *= n / o
            except (TypeError, ValueError):
                errs.append(f"{name} scales compute but its current value is unknown: set default:")
    ms = float(jc.get("max_scale", 2.0))
    if scale > ms:
        errs.append(f"over budget: this change scales compute x{scale:.2f}, max x{ms:g}")
    return errs


def brief(p: Project, cfg: dict, job: str, jc: dict, text: str, tr: dict, why: list[str]) -> dict:
    knobs = jc.get("knobs") or {}
    t = cfg.get("tune") or {}
    base = family(job)
    tried = _tries(p, base)
    return {
        "job": job,
        "triggered_by": why,
        "metric": jc.get("metric", "success"),
        "target": jc.get("target"),
        "contract": [clip(l.strip(), 200) for l in text.splitlines() if l.strip().startswith("#@")][:20],
        "trend": {k: v for k, v in tr.items() if not k.startswith("_")},
        "points": tr.get("_points") or [],
        "knobs": {n: {**{k: v for k, v in kc.items() if k != "flag"}, **({"flag": kc["flag"]} if kc.get("flag") else {}),
                      "current": current(text, n, kc)} for n, kc in knobs.items()},
        "max_scale": jc.get("max_scale", 2.0),
        "tries_left": max(0, int(t.get("max_per_job", 2)) - len([r for r in tried if r.get("called")])),
        "do_not_repeat": sorted({r["change"] for r in tried if r.get("change")})
        + ledger.do_not_repeat(p, limit=10),
    }


def maybe(p: Project, cfg: dict, job: str, text: str, ended: bool) -> str | None:
    """Call `tune` for this job if a trigger fires and the caps allow. Returns
    what was applied (e.g. 'tune imitate_t1'), or None when nothing was called."""
    jc = job_cfg(cfg, job)
    if not jc:
        return None
    t = cfg["tune"]
    st = trend.load(p, job)
    if not st:
        return None
    tr = trend.compact_state(st)
    metric = jc.get("metric", "success")
    name = metric if metric in st.get("series", {}) else next(
        (k for k, v in st.get("series", {}).items() if v["kind"] == "success"), metric)
    tr["_points"] = trend.points(p, job, name, int(t.get("points", 20)))
    why = triggers(cfg, jc, tr)
    done_on = set(st.get("tuned_on") or [])
    why = [w for w in why if w not in done_on]
    if not why:
        return None
    base = family(job)
    calls = [r for r in _tries(p, base) if r.get("called")]
    if len(calls) >= int(t.get("max_per_job", 2)):
        return _mark(p, job, st, why, None)
    if _today(p) >= int(t.get("max_per_day", 6)):
        return _mark(p, job, st, why, None)
    for g in (cfg.get("hooks") or {}).get("protected_paths") or []:
        if fnmatch.fnmatch(job, g):
            return None
    knobs = jc.get("knobs") or {}
    cur = {n: current(text, n, k) for n, k in knobs.items()}
    tried = {r["change"].lower() for r in _tries(p, base) if r.get("change")}
    tried |= {r["change"].lower() for r in ledger.entries(p) if r.get("verdict") == "fail"
              and r.get("parent") == base and r.get("change")}
    prot = (cfg.get("hooks") or {}).get("protected_paths") or []
    b = brief(p, cfg, job, jc, text, tr, why)
    _mark(p, job, st, why, None)
    try:
        ans = router.call(p, cfg, "tune", b, validate=lambda a: check(a, jc, cur, tried, prot),
                          max_tier=t.get("max_tier", "T3"))
    except router.BudgetExceeded as e:
        ledger.record(p, "tune", job, "unknown", note=f"budget: {e}", parent=base, called=True)
        return "tune: budget"
    except router.CallFailed as e:
        owner_note(p, f"tune answer for {job} rejected", f"{clip(str(e), 600)}. Trend: {trend.line(job, tr)}")
        ledger.record(p, "tune", job, "unknown", note=f"rejected: {clip(str(e), 300)}", parent=base,
                      called=True)
        return "tune: rejected -> owner queue"
    if ans["action"] == "none":
        ledger.record(p, "tune", job, "same", note=f"none: {ans.get('why', '')}", parent=base, called=True)
        return "tune: no change"
    changes = {c["knob"]: str(c["value"]) for c in ans["changes"]}
    new = apply(p, cfg, job, text, changes, jc, ans.get("why", ""), why, ended)
    return f"tune {new}"


def _mark(p, job, st, why, res):
    st["tuned_on"] = sorted(set(st.get("tuned_on") or []) | set(why))
    write_json(trend._path(p, job), st)
    return res


def _next_name(p: Project, base: str) -> str:
    n = 1 + sum(1 for r in _tries(p, base) if r.get("applied"))
    while any((p.q(part) / f"{base}_t{n}.job").exists() for part in ("pending", "running", "done")) \
            or (p.q("remote") / f"{base}_t{n}.json").exists():
        n += 1
    return f"{base}_t{n}"


def apply(p: Project, cfg: dict, job: str, text: str, changes: dict, jc: dict, why: str,
          trig: list[str], ended: bool) -> str:
    from . import watch
    base = family(job)
    new = _next_name(p, base)
    (p.q("pending") / f"{new}.job").write_text(apply_knobs(text, changes, jc["knobs"]))
    key = change_key(changes)
    write_json(tdir(p) / f"{new}.json", {"job": new, "from": job, "parent": base, "change": key,
                                         "changes": changes, "ts": now(), "trigger": trig})
    events.emit(p, "submit", new, tune_of=job, change=key)
    ledger.record(p, "tune", new, "unknown", note=f"{','.join(trig)}: {why}", change=key,
                  parent=base, called=True, applied=True, source=job,
                  running=not ended)
    watch.copy(p, job, new)
    if not ended:   # never stopped by code: a running job is the owner's to stop
        owner_note(p, f"{job} still running", f"tuned copy {new} queued ({key}): stop {job} "
                   "if both should not run.")
    return new


def tdir(p: Project) -> Path:
    d = p.state / "tune"
    d.mkdir(parents=True, exist_ok=True)
    return d


def outcome(p: Project, job: str, passed: bool, note: str = "") -> None:
    """A tuned job ended: its change is a win or a dead end."""
    f = tdir(p) / f"{job}.json"
    if not f.exists():
        return
    m = json.loads(f.read_text())
    if m.get("verdict"):
        return
    v = "win" if passed else "fail"
    ledger.record(p, "tune", job, v, note=f"tuned run {'passed' if passed else 'failed'}: {clip(note, 200)}",
                  change=m["change"], parent=m["parent"])
    m["verdict"] = v
    write_json(f, m)


def scan(p: Project, cfg: dict, remote=None) -> list[str]:
    """Running jobs: tune any whose trend matches a trigger."""
    if not (cfg.get("tune") or {}).get("enabled"):
        return []
    out = []
    jobs = {}
    if remote is not None:
        for j, r in remote.tracked().items():
            if r.get("state") == "running" and r.get("src") and Path(r["src"]).exists():
                jobs[j] = Path(r["src"]).read_text()
    else:
        for f in p.q("running").glob("*.job"):
            jobs[f.stem] = f.read_text()
    for j, text in jobs.items():
        r = maybe(p, cfg, j, text, ended=False)
        if r:
            out.append(f"{j}: {r}")
    return out
