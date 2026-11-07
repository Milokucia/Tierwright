"""Checks: how a machine tells a step or goal is done, in plain language.

A gate or done-bar item is a string (or the same thing as a one-key dict):

    file: out/newsletter.md                    exists and is not empty (globs ok)
    words: out/newsletter.md 400-600           word count in range (also >=N, <=N)
    contains: out/newsletter.md: Acme, acme.com/demo     every term present (any case)
    lacks: out/newsletter.md: lorem, TODO      no term present
    review: out/newsletter.md against brand.md [: extra criteria]
                                               the judge role (T1) passes it against
                                               the guide; fails cite the guide
    run: pytest -q                             a shell command must exit 0
    pytest -q                                  anything else is a shell command

Every check returns {"cmd", "rc", "s", "tail"} like a shell gate, so steps,
the done bar and the plan validator treat them alike.
"""

from __future__ import annotations

import glob
import re
import subprocess
from pathlib import Path

from .project import Project, now

KINDS = ("file", "words", "contains", "lacks", "review", "run")
MAX_DOC = 20000


class CheckError(ValueError):
    pass


def normalize(item) -> str:
    if isinstance(item, dict):
        if len(item) != 1:
            raise CheckError(f"a check is one key, got {sorted(item)}")
        (k, v), = item.items()
        if k not in KINDS:
            raise CheckError(f"unknown check {k!r} (one of {', '.join(KINDS)})")
        return f"{k}: {v}"
    if not isinstance(item, str) or not item.strip():
        raise CheckError("a check is a non-empty string")
    return item.strip()


def parse(item) -> dict:
    s = normalize(item)
    m = re.match(r"^(\w+):\s*(.*)$", s, re.S)
    if not m or m.group(1) not in KINDS:
        return {"kind": "run", "cmd": s}
    kind, rest = m.group(1), m.group(2).strip()
    if kind == "run":
        if not rest:
            raise CheckError("run: needs a command")
        return {"kind": "run", "cmd": rest}
    if kind == "file":
        if not rest:
            raise CheckError("file: needs a path")
        return {"kind": "file", "path": rest}
    if kind == "words":
        m2 = re.match(r"^(.+?)\s+(?:(\d+)\s*-\s*(\d+)|>=\s*(\d+)|<=\s*(\d+))$", rest)
        if not m2:
            raise CheckError("words: PATH MIN-MAX (or >=N / <=N)")
        lo = m2.group(2) or m2.group(4)
        hi = m2.group(3) or m2.group(5)
        return {"kind": "words", "path": m2.group(1).strip(),
                "min": int(lo) if lo else None, "max": int(hi) if hi else None}
    if kind in ("contains", "lacks"):
        if ":" not in rest:
            raise CheckError(f"{kind}: PATH: term, term")
        path, terms = rest.split(":", 1)
        terms = [t.strip() for t in terms.split(",") if t.strip()]
        if not path.strip() or not terms:
            raise CheckError(f"{kind}: PATH: term, term")
        return {"kind": kind, "path": path.strip(), "terms": terms}
    # review
    m2 = re.match(r"^(.+?)\s+against\s+(\S+)\s*(?::\s*(.*))?$", rest, re.S)
    if not m2:
        raise CheckError("review: PATH against GUIDE [: criteria]")
    return {"kind": "review", "path": m2.group(1).strip(), "guide": m2.group(2).strip(),
            "criteria": (m2.group(3) or "").strip()}


def problems(p: Project, item, planned: set[str] | None = None) -> str | None:
    """Why this check cannot run as written (for plan validation), or None."""
    try:
        c = parse(item)
    except CheckError as e:
        return str(e)
    if c["kind"] == "review":
        g = c["guide"]
        if not (p.root / g).exists() and not (planned and g in planned):
            return f"review guide {g} does not exist"
    return None


def _files(p: Project, pat: str) -> list[Path]:
    return [Path(f) for f in sorted(glob.glob(str(p.root / pat)))]


def _read(p: Project, pat: str) -> tuple[str, str | None]:
    fs = [f for f in _files(p, pat) if f.is_file()]
    if not fs:
        return "", f"{pat}: no such file"
    return "\n\n".join(f.read_text(errors="replace") for f in fs), None


def run(p: Project, item, cfg: dict | None = None, timeout: float = 1800) -> dict:
    t0 = now()
    label = normalize(item)
    try:
        c = parse(item)
    except CheckError as e:
        return {"cmd": label, "rc": 2, "s": 0.0, "tail": [str(e)]}

    def res(ok: bool, *tail):
        return {"cmd": label, "rc": 0 if ok else 1, "s": round(now() - t0, 1), "tail": [t for t in tail if t]}

    k = c["kind"]
    if k == "run":
        try:
            pr = subprocess.run(c["cmd"], shell=True, cwd=p.root, capture_output=True, text=True,
                                timeout=timeout)
            return {"cmd": label, "rc": pr.returncode, "s": round(now() - t0, 1),
                    "tail": (pr.stdout + pr.stderr).strip().splitlines()[-8:]}
        except subprocess.TimeoutExpired:
            return {"cmd": label, "rc": 124, "s": round(now() - t0, 1), "tail": [f"timeout after {timeout}s"]}
    if k == "file":
        fs = [f for f in _files(p, c["path"]) if f.exists() and (f.is_dir() or f.stat().st_size > 0)]
        return res(bool(fs), "" if fs else f"{c['path']}: missing or empty")
    text, err = _read(p, c["path"])
    if err:
        return res(False, err)
    if k == "words":
        n = len(re.findall(r"\b[\w'’-]+\b", text))
        ok = (c["min"] is None or n >= c["min"]) and (c["max"] is None or n <= c["max"])
        return res(ok, f"{n} words")
    low = text.lower()
    if k == "contains":
        miss = [t for t in c["terms"] if t.lower() not in low]
        return res(not miss, f"missing: {', '.join(miss)}" if miss else "all present")
    if k == "lacks":
        hit = [t for t in c["terms"] if t.lower() in low]
        return res(not hit, f"found: {', '.join(hit)}" if hit else "none present")
    # review: a model judges, code decides what counts as a pass
    from . import config, router
    guide, gerr = _read(p, c["guide"])
    if gerr:
        return res(False, gerr)
    cfg = cfg or config.load(p)
    try:
        v = router.call(p, cfg, "judge", {
            "document_path": c["path"], "document": text[:MAX_DOC],
            "guide_path": c["guide"], "guide": guide[:MAX_DOC], "extra_criteria": c["criteria"]},
            validate=lambda o: [] if o["verdict"] == "pass" or o["reasons"] else
            ["a fail must give at least one reason citing the guide"])
    except (router.BudgetExceeded, router.CallFailed) as e:
        return res(False, f"review could not run: {e}")
    return res(v["verdict"] == "pass", *[f"- {r}" for r in v["reasons"][:6]])
