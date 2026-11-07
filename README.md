# Tierwright

**An agent harness that spends your model budget on building, not babysitting.**

You state a goal and how a machine can tell it is done. Tierwright reads your
codebase, writes a validated step-by-step plan from what exists to that goal,
and runs the work with infrastructure that checks itself. Code does
everything a rule can decide. When judgment is needed, the cheapest model
that holds quality is called with a small JSON brief, and its answer is
validated before anything is applied.

```
pip install tierwright            # add [api] for the Anthropic API backend
cd your-project
tw init                           # asks: subscription, API, or both? budgets? goal?
tw adopt                          # codebase + goal -> steps/ and GAP.md
tw step show                      # the one step to build now
tw step check                     # run its gate; passing unlocks the next
tw status                         # the whole project in <= 20 lines
```

Until it is on PyPI: `pip install git+https://github.com/Milokucia/Tierwright`.

## Why

Left alone, an AI harness spends tokens on work code could do: polling jobs,
reading raw logs, re-diagnosing yesterday's failure, retrying a dead end,
running every role on the top model. Tierwright is built from a production
agent pipeline where moving those rules into code removed whole classes of
model calls. It ships the guardrails that pipeline had to learn.

## The four rules

1. **Build as you go, starting from the goal.** The first step moves the done
   bar. Each step is locked until the one before it passes its gate. Later
   steps are re-planned with what was learned (`tw adopt --refresh`).
2. **Infrastructure checks itself.** Jobs carry output contracts and are
   stopped the moment their outputs go wrong. A dispatcher turns every job end
   into a verdict through a rule table, with no model call when a rule
   matches.
3. **The right model for the task.** Each role has a declared tier:
   - **T0:** code, no model.
   - **T1:** Haiku, for extraction and classification.
   - **T2:** Sonnet, for diagnosis and review.
   - **T3:** Opus, for design and building.

   A role moves up a tier only after rejected answers, and every escalation
   is logged.
4. **Validation loops on everything.** Steps have gates, jobs have contracts,
   model answers have schemas plus caller checks, plans are validated before
   you see them, and a new rule is backtested before it is saved.

## Backends: subscription, API, or both

`tw init` asks and writes `backend:` to `tierwright.yaml`.

| Choice | How agents run | Trade-off |
|---|---|---|
| `subscription` | headless `claude -p` on your Claude plan, with `--json-schema` output | no per-token bill; each call carries Claude Code's system prefix (cached after the first call) |
| `api` | Anthropic Messages API through the official SDK, cached role prefixes, structured output | pay per token; cheapest for high-volume short calls |
| `both` | builders, the planner and diagnosis on the plan; triage, extraction, reports and reviews on the API | both budgets apply; override any role in `roles:` |

Budgets are set per call, per day and per goal. When a backend is over
budget, a call under `both` moves to the other backend and the ledger records
the move. Otherwise the call waits and `owner-queue.md` says why. Spending is
never silent. `tw init` makes one tiny probe call per chosen backend
(`--no-probe` skips it).

## How it fits together

```
goal.yaml ──> tw adopt ──> steps/NNN-*.yaml (locked in order) ──> tw step check (gate)
                                   │
job with #@ contract ──> tw submit ──> tw queue run ──> events.jsonl
                                                          │
          dispatcher: brief ──> rule table ──match──> ok / ignore / resubmit-once
                                    │ no match
                                    v
                    router ──> diagnose role (T2) ──> JSON proposal
                                    │
               schema + whitelist + do-not-repeat check ──> applied, or owner-queue.md
```

## `tw adopt`: from your codebase to exact steps

1. **Inventory (T0):** languages, manifests, test command, CI, agent files,
   churn.
2. **Fit (T0):** which parts of the approach your code already has, with file
   names, so the plan integrates them instead of duplicating them.
3. **Repo map (T1):** one summary per module, cached by content hash. A
   re-run summarizes only the files that changed.
4. **Gap plan (T3):** one planner call over the goal, inventory, fit, map and
   the do-not-repeat list. It never reads raw source.
5. **Validation (T0):**
   - files to `edit` or `delete` exist, and files to `add` don't;
   - every gate command resolves;
   - dependencies point only to earlier steps;
   - no step touches protected paths or repeats a dead end.

   A rejected plan goes back with the exact errors, and then up a tier.
6. **Output:** `steps/` and `GAP.md`.

`tw adopt --no-model` builds the plan from the fit table alone, at zero cost.

## `tw mine`: build the infrastructure your agents use most

Agents pay again for the same work in every session: the same three status
commands, the same 9k-token file read, the same unmatched failure diagnosed
again. `tw mine` reads this project's Claude Code transcripts
(`~/.claude/projects/<project>/`) and Tierwright's own records, ranks those
repeats by estimated tokens, and turns them into code:

| Seen in transcripts | Becomes | Checked by |
|---|---|---|
| the same command sequence, 3 or more times | `tw tool NAME`: one call, output trimmed | read-only commands only; the script and the originals both run, and the script must carry each original's output |
| the same large file read again and again | `tw digest PATH`: a cached T1 summary, regenerated when the file changes | the extract role's schema |
| the same unmatched failure, twice | a triage rule | backtest: matches its incidents, flips no job that finished ok |
| polling, or a role whose answers keep getting rejected | a note in MINE.md | needs a person |

```
tw mine                 # writes MINE.md: ID, kind, times seen, est. tokens saved, valid
tw mine apply ID        # install one; adds a line to CLAUDE.md and the ledger
tw tool                 # list installed tools; the next `tw mine` reports how often each was used
```

Nothing is installed unless it passes its check. A tool whose commands are
not read-only is shown but never installed automatically (`--force` installs
it unverified).

## Output contracts

Every job file states what its outputs must look like (`tw contract` prints
the full reference):

```bash
#@ timeout 2h
#@ progress steps re='step (\d+)' stall=10m warmup=5m
#@ value loss re='loss (\S+)' finite
#@ forbid oom re='CUDA out of memory'
#@ artifact out/model.pt min_size=1000
#@ result out/eval.json keys=success_rate,episodes
#@ success eval re='passed (\d+)/(\d+)' min=0.8
python train.py
```

A job without a contract is refused. A job that is still running but writes
nothing, or writes garbage, fails its contract and is stopped.

## Guardrails (Claude Code hooks, installed by `tw init`)

| Trap | Guard |
|---|---|
| Agent polls a long job | `sleep` of 20 s or more, `watch`, `tail -f` and sleep loops are blocked |
| Agent reads raw logs | `.tierwright/logs/` is blocked; use `tw brief JOB` |
| Grader or held-out set edited | `hooks.protected_paths` globs are blocked for Edit and Write |
| Same failure diagnosed again | rule table, plus `tw rule add --job INCIDENT` (backtested) |
| Dead end retried | the ledger's do-not-repeat list blocks the proposal |
| Everything on the top model | tiered roles, and subagents pinned to haiku, sonnet or opus |
| Silent spend | per-call, per-day and per-goal budgets, plus the owner queue |
| Context lost between sessions | `tw status` writes `HANDOFF.md` |

## Commands

```
tw init / adopt / plan / step show|list|check / done
tw submit JOB / queue run [--watch] / queue stop NAME / dispatch / brief JOB
tw contract [lint FILE...]   tw rule list|add|backtest   tw ask ROLE [FILE]
tw route [ROLE]   tw spend   tw ledger [dnr]   tw status
tw mine [apply ID]   tw tool [NAME]   tw digest PATH
```

A walk-through with no model calls is in `examples/hello/`.

## Project files

| Path | Committed | What |
|---|---|---|
| `tierwright.yaml` | yes | backend, budgets, tiers, role overrides, hooks |
| `goal.yaml` | yes | the goal and its done bar |
| `steps/` | yes | the step ladder |
| `.tierwright/ledger.jsonl` | yes | every attempt and verdict |
| `.tierwright/rules.yaml`, `roles/` | yes | project triage rules, frozen role prompts |
| `.tierwright/tools/`, `tools.yaml`, `digests/` | yes | mined tools and digests |
| `GAP.md`, `STATUS.md`, `HANDOFF.md`, `MINE.md`, `owner-queue.md` | your call | written by code |
| `.tierwright/{queue,logs,briefs,runs,attention}` | no | runtime state |

## Develop

```
pip install -e '.[dev]'
python3 -m pytest -q        # ~2 s, no network, no model calls (fake backend)
```

MIT licensed. See `docs/principles.md` for the principles and the failures
behind each.

## Not in v0.1 yet

- Moving a role down a tier after replaying recorded briefs on the cheaper
  model.
- Batch API for reviews.
- Remote or GPU queue backends.
- A builder that runs in a git worktree.
- Mined tools that take arguments, for commands that differ only by one
  value.
