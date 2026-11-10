# CLAUDE.md

Tierwright's own repo. Python 3.10+, one dependency (pyyaml); the API backend
optionally uses the `anthropic` SDK.

## Response style

Be short. Answer, then stop. Say what changed and what to run.

## Layout

`tierwright/`: one module per component.

| Module | What it does |
|---|---|
| `contract` | output contracts |
| `jobqueue` | the job queue |
| `remote` | ssh queue backend: submit to a box, read its ledger into events |
| `events` | the event log |
| `brief` | job briefs for rules and models |
| `rules` | the triage table |
| `dispatch` | the whitelist dispatcher |
| `trend` | running jobs' progress / value / success points, offset-tracked |
| `watch` | `tw watch` conditions, attention files, Pushover |
| `tune` | the `tune` role: triggers, knob checks, tuned resubmits |
| `usage` | Claude Code agent tokens from transcripts (`tw spend`) |
| `router` | tiers, budgets and escalation |
| `backends` | subscription, api and fake backends |
| `schemas` | role output schemas |
| `steps` | goal and step ladder |
| `checks` | plain-language checks (file, words, contains, lacks, review, run) |
| `adopt` | inventory, fit, repo map and plan |
| `mine` | transcripts -> mined tools, digests and rules |
| `hooks` | Claude Code guards |
| `init` | `tw init` |
| `status` | `tw status` |
| `cli` | the `tw` command |

Templates (role prompts, subagents, the CLAUDE.md section) live in
`tierwright/templates/`.

## Rules

- `python3 -m pytest -q` before every commit (~2 s, no network: the fake backend via `TIERWRIGHT_FAKE`).
- A new component ships with its validation loop and a test of the loop rejecting bad input.
- Model ids live only in `config.DEFAULTS` and the generated `tierwright.yaml`.
- Never call a model in tests. Never add a dependency to the core.
- When behaviour changes, update README.md in the same commit.
