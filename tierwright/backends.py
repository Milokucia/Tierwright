"""Where a model call runs. Each backend takes a frozen system prefix (cached),
a variable input, and a JSON schema, and returns a parsed object plus usage.

    subscription   headless `claude -p` on the user's plan
    api            Anthropic Messages API via the official SDK (pip install tierwright[api])
    fake           canned answers for tests and dry runs (TIERWRIGHT_FAKE=path.json)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field


class BackendError(Exception):
    pass


@dataclass
class Result:
    output: object
    text: str
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    error: str | None = None


def parse_json(text: str):
    """The model's answer as JSON: the whole text, a fenced block, or the
    outermost {...}."""
    s = text.strip()
    for cand in (s, *re.findall(r"```(?:json)?\s*(.*?)```", s, re.S)):
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            pass
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(s[i: j + 1])
        except json.JSONDecodeError:
            pass
    raise BackendError("answer is not JSON")


def price(pricing: dict, model: str, usage: dict) -> float:
    pin, pout = pricing.get(model, [0.0, 0.0])
    tin = usage.get("input_tokens", 0) + 1.25 * usage.get("cache_creation_input_tokens", 0) \
        + 0.1 * usage.get("cache_read_input_tokens", 0)
    return (tin * pin + usage.get("output_tokens", 0) * pout) / 1e6


class Subscription:
    name = "subscription"

    def __init__(self, binary: str | None = None):
        self.binary = binary or os.environ.get("TIERWRIGHT_CLAUDE", "claude")

    def available(self) -> str | None:
        return None if shutil.which(self.binary) else f"`{self.binary}` CLI not on PATH"

    def call(self, *, model, effort, system, user, schema, timeout, tools=(), cwd=None,
             max_budget_usd=None, pricing=None, role="") -> Result:
        cmd = [self.binary, "-p", "--model", model, "--output-format", "json",
               "--effort", effort, "--append-system-prompt", system,
               "--json-schema", json.dumps(schema)]
        if tools:
            cmd += ["--allowedTools", *tools]
        else:  # a judgment call on a brief needs no tools; reading would only spend tokens
            cmd += ["--disallowedTools", "Bash", "Edit", "Write", "Read", "Glob", "Grep",
                    "WebFetch", "WebSearch", "Task", "NotebookEdit"]
        if max_budget_usd:
            cmd += ["--max-budget-usd", str(max_budget_usd)]
        try:
            pr = subprocess.run(cmd, input=user, capture_output=True, text=True,
                                timeout=timeout, cwd=cwd)
        except subprocess.TimeoutExpired:
            return Result(None, "", error=f"timeout after {timeout}s")
        except OSError as e:
            return Result(None, "", error=f"cannot run {self.binary}: {e}")
        if pr.returncode != 0 and not pr.stdout.strip():
            return Result(None, "", error=f"claude exited {pr.returncode}: {pr.stderr.strip()[-300:]}")
        try:
            env = json.loads(pr.stdout)
        except json.JSONDecodeError:
            return Result(None, pr.stdout, error="claude -p did not return JSON")
        usage = env.get("usage") or {}
        cost = float(env.get("total_cost_usd") or 0.0)
        text = env.get("result") or ""
        if env.get("is_error"):
            return Result(None, text, usage, cost, error=f"claude -p error: {str(text)[:300]}")
        out = env.get("structured_output")
        if out is None:
            try:
                out = parse_json(text)
            except BackendError as e:
                return Result(None, text, usage, cost, error=str(e))
        return Result(out, text, usage, cost)


class Api:
    name = "api"

    def available(self) -> str | None:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return "anthropic SDK not installed: pip install 'tierwright[api]'"
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("ANTHROPIC_PROFILE")
                or os.path.isdir(os.path.expanduser("~/.config/anthropic"))):
            return "no API credential: set ANTHROPIC_API_KEY or run `ant auth login`"
        return None

    def call(self, *, model, effort, system, user, schema, timeout, tools=(), cwd=None,
             max_budget_usd=None, pricing=None, role="") -> Result:
        import anthropic

        client = anthropic.Anthropic(timeout=float(timeout))
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=16000,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={"effort": effort,
                               "format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.RateLimitError as e:
            return Result(None, "", error=f"rate limited: {e.message}")
        except anthropic.APIStatusError as e:
            return Result(None, "", error=f"API {e.status_code}: {e.message}")
        except anthropic.APIConnectionError as e:
            return Result(None, "", error=f"connection: {e}")
        u = resp.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_read_input_tokens": u.cache_read_input_tokens or 0,
                 "cache_creation_input_tokens": u.cache_creation_input_tokens or 0}
        cost = price(pricing or {}, model, usage)
        if resp.stop_reason == "refusal":
            return Result(None, "", usage, cost, error="refusal")
        if resp.stop_reason == "max_tokens":
            return Result(None, "", usage, cost, error="hit max_tokens")
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            return Result(parse_json(text), text, usage, cost)
        except BackendError as e:
            return Result(None, text, usage, cost, error=str(e))


class Fake:
    """Answers from a JSON file: {"role": answer | [answer, answer, ...]}.
    A list is consumed in order (to test retries and escalation)."""

    name = "fake"

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("TIERWRIGHT_FAKE")
        self._used: dict[str, int] = {}

    def available(self) -> str | None:
        return None if self.path and os.path.exists(self.path) else "TIERWRIGHT_FAKE not set"

    def call(self, *, model, effort, system, user, schema, timeout, role="", **kw) -> Result:
        data = json.loads(open(self.path).read())
        ans = data.get(role, data.get("*"))
        if isinstance(ans, list):
            i = self._used.get(role, 0)
            self._used[role] = i + 1
            ans = ans[min(i, len(ans) - 1)]
        if ans is None:
            return Result(None, "", error=f"fake has no answer for {role}")
        usage = {"input_tokens": len(system + user) // 4, "output_tokens": len(json.dumps(ans)) // 4}
        return Result(ans, json.dumps(ans), usage, 0.0)


_FAKE: Fake | None = None


def get(name: str):
    global _FAKE
    if os.environ.get("TIERWRIGHT_FAKE"):
        if _FAKE is None or _FAKE.path != os.environ["TIERWRIGHT_FAKE"]:
            _FAKE = Fake()
        return _FAKE
    if name == "subscription":
        return Subscription()
    if name == "api":
        return Api()
    raise BackendError(f"unknown backend {name}")
