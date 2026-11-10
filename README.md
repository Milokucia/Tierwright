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

## Lite mode, for work that isn't code

`tw init --lite` keeps the model tiers, budgets, step ladder and `tw mine`,
and drops job contracts. Goals and steps are checked in plain language:

```yaml
done:
  - "file: out/newsletter.md"
  - "words: out/newsletter.md 300-500"
  - "contains: out/newsletter.md: acme.com/demo"
  - "lacks: out/newsletter.md: revolutionary, game-changing"
  - "review: out/newsletter.md against brand.md: one call to action"
```

`review:` asks the judge role (Haiku) for a pass or fail against your guide,
and every fail must cite a rule. The guide for non-engineers is
`docs/lite.md`, and a worked example is in `examples/newsletter/`. These
checks also work in full mode, mixed with shell commands.

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

running jobs ──> tw queue run reads their new output ──> trend (last, slope, stall, success)
                         │                                   │
           tw watch conditions ──> attention + Pushover      └─ tune trigger ──> tune role (T2)
                                                                    │
                       knob whitelist + range + budget + do-not-repeat ──> <job>_tN, or owner-queue.md
```

## No watchers: trends and `tw watch`

An agent that waits on a training run pays for every poll. Tierwright moves
the waiting into the dispatcher, which a timer runs (`tw queue run` every few
minutes):

- **Trends.** Each run reads the bytes every running job wrote since the last
  run (byte offsets per source, so nothing is read twice) and applies the
  job's own `#@ progress`, `#@ value` and `#@ success` regexes. Sources are the
  job's stdout (locally, or `queue.log_dir` on an ssh host), `in=ledger` (the
  queue ledger lines for that job), or `in=PATH`; `${VAR}` comes from
  `#@ var NAME re=RE` matches, and `pct` divides percents by 100. `tw brief JOB`
  then carries a compact `trend` (last value, slope per hour, peak, drop from
  peak, stall, success so far), also for a job still running.
- **`tw watch JOB --until 'success>=0.9|stalled|ended'`** registers a condition
  and returns at once. Clauses: `ended`, `stalled`, or `METRIC OP NUMBER` with
  METRIC `success`, any series name, `NAME.slope`, `NAME.peak`, `NAME.drop`,
  `NAME.n` or `age_h`. When one holds, the dispatcher writes
  `.tierwright/attention/watch-<job>.json`, a ledger line, and a Pushover
  message if `PUSHOVER_TOKEN` / `PUSHOVER_USER` are set (environment, or
  KEY=VALUE lines in `~/.pushover`; names in `notify.pushover`). A job that
  ends with no clause true fires too, so no watch outlives its job. On an ssh
  queue, a job queued by the host's own script is adopted from its running or
  pending dir. JOB may be a glob (`tw watch 'dfp_*' --until ...`): every
  matching job fires once, the watch stays, and the glob joins `queue.adopt`.
- The polling guard tells an agent that tries `sleep`, `watch` or a sleep
  loop to use `tw watch` instead.

## Live-run tuning without an agent: the `tune` role

When a running or ended job's trend matches a trigger, code calls the `tune`
role once (T2, on the API with `backend: both`) with a small JSON brief: the
contract lines, the trend, the last `points` values of the watched metric, the
job's knob whitelist with current values, and the do-not-repeat list.

```yaml
tune:
  enabled: true
  max_per_job: 2        # tune calls per job family (job, job_t1, job_r1 ...)
  max_per_day: 6
  points: 20
  max_tier: T3          # rejected answers escalate, never past this
  triggers:
    stall: true                              # progress stopped
    plateau: {window: 5, eps: 0.01}          # flat, below target
    decay: {drop: 0.1, min_points: 2}        # fell this far from its peak
  jobs:
    - match: "imit_*"                        # fnmatch on the job name
      metric: success
      target: 0.9
      max_scale: 2.0                         # compute budget for `scales` knobs
      knobs:
        PATIENCE: {type: int, min: 1, max: 6, default: 3}
        DART_NOISE: {type: choice, choices: ["0.2 0.4", "0.1 0.2"], default: "0.2 0.4"}
        REC_ENVS: {type: int, min: 256, max: 4096, default: 1024, scales: true}
        lr: {flag: --lr, type: float, min: 1.0e-5, max: 1.0e-2}
```

The answer is schema-checked, then checked by code: knobs only from the
whitelist, values in range or choices and different from now, no protected
path, not a change set already tried on this job, and the product of
new/current for `scales` knobs within `max_scale`. A rejected answer goes back
with the errors and moves up a tier after two (the router); after the last
tier it goes to `owner-queue.md`. A valid change is resubmitted as
`<job>_tN` with the env assignments or flags rewritten (an env knob the job
never sets gets an `export` after its header block) and recorded in the ledger
(kind `tune`). When the tuned job ends, a pass records `win` and a fail puts
the change on the do-not-repeat list. A running original is never stopped by
code: an owner-queue line says the tuned copy is queued. An ended job that is
tuned is not also diagnosed.

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

## Remote GPU queue (ssh)

When jobs must run on another machine (a GPU box with its own queue and its
own contract verifier), point `queue:` in `tierwright.yaml` at it:

```yaml
queue:
  backend: ssh                  # local (default) | ssh
  host: ubuntu@10.0.0.5
  key: ~/box.pem
  pending_dir: ~/gq/pending     # jobs dropped here run in name order
  running_dir: ~/gq/running
  done_dir: ~/gq/done
  ledger: ~/fill_ledger.txt     # the box's append-only result lines
  submit_cmd: null              # or the box's own tag/lint/copy script, e.g.
                                # "BOX={host} KEY={key} bash cloud/gq_submit.sh {file} --as {name}"
  events:                       # ledger line -> event; each needs (?P<job>...), rc from (?P<rc>...)
    start: 'GQ start (?P<job>\S+?)(?:\.job)? on GPU (?P<gpu>\d+)'
    end:   'GQ end (?P<job>\S+?)(?:\.job)? rc=(?P<rc>-?\d+)'
    stop:  'GQ HEALTH stop (?P<job>\S+?)(?:\.job)?: (?P<detail>.*)'
    pass:  'GQ CONTRACT PASS (?P<job>\S+?)(?:\.job)? rc=(?P<rc>-?\d+)'
    fail:  'GQ CONTRACT FAIL (?P<job>\S+?)(?:\.job)? rc=(?P<rc>-?\d+): (?P<detail>.*)'
    skip:  'GQ CONTRACT SKIP (?P<job>\S+?)(?:\.job)? rc=(?P<rc>-?\S+): (?P<detail>.*)'
  translate:                    # Tierwright-only #@ lines -> the box's headers (null = strip + warn)
    timeout: '# MAX_H={hours}'  # or {minutes} / {seconds}
    after: '# AFTER=~/gq/marks/{job}.ok'
  header_lines: 3               # the box reads headers from this many first lines
  adopt: ['dfp_*', 'imit*']     # also track jobs the box's own scripts queue (null: tune.jobs globs)
  log_dir: ~/gq/logs            # running jobs' stdout, read for trends (null: ledger lines only)
  log_name: '{job}.job.log'
```

- `tw submit JOB [--name N] [-- ARGS]` lints the contract here, then copies
  the job to `pending_dir` with `scp` (refused if the name is already pending,
  running or done there), or runs `submit_cmd` with ARGS appended.
- `#@ timeout` and `#@ after` are checked by Tierwright's local queue only, so
  `tw submit` rewrites them as the box's header lines (`translate:`). The job's
  own headers stay in the first `header_lines`. Two of either directive, one
  that clashes with the job's own header, or headers pushed past the lines the
  box reads are refused before anything is sent.
- `tw queue run` never runs a job here. It sends the dispatcher's resubmits
  to the box, reads the ledger lines added since the last run (a byte offset,
  so nothing is read twice) and writes the same `start` / `end` events a local
  run writes. The dispatcher, the rules and `tw brief JOB` work unchanged; a
  brief's tail is the job's own ledger lines (by name or `gq-<name>` tag).
- `tw queue stop NAME` removes a job that is still pending on the box.
- Jobs submitted from this project are tracked, and so are jobs whose name
  matches a `queue.adopt` glob (default: the `tune.jobs` match patterns): a
  ledger `GQ start NAME` line, or the job already in `running_dir`, adopts it
  (its file is read from the box), and from then on it gets a trend, a
  contract verdict, the dispatcher's rules and tune like any other. A job ends at its `pass`
  or `fail` line; a `stop` line before a fail makes it a contract stop. A
  `skip` line (stopped by the queue, e.g. preempted) ends it as `preempted`:
  the `preempted` rule resubmits it once, and the ledger does not count it as
  a failure.
- An unreachable host or a bad `queue:` block fails with one clear line.
  It shells out to `ssh` / `scp`; nothing new to install.

The box enforces its own `#@` dialect, so with an ssh queue local lint warns
on words it does not know (`hard`, `pct`, `ref=`, ...) instead of refusing the
job. Declare them to silence the warnings, or set `unknown: error`:

```yaml
contract:
  unknown: auto                 # auto = warn with an ssh queue, error locally
  kinds: [tag, var]
  flags: [hard, pct]
  opts: {success: [ref, tol, agg], value: [key], artifact: [at, if]}
```

A declared alternative also satisfies a required option: `success` needs
`min=`, or a declared `ref=` floor or `max=` ceiling; `value` needs `re=`, or a
declared `tag=` or `key=`. The local verifier skips checks in those forms.

## Guardrails (Claude Code hooks, installed by `tw init`)

| Trap | Guard |
|---|---|
| Agent polls a long job | `sleep` of 20 s or more, `watch`, `tail -f` and sleep loops are blocked, with "use tw watch" |
| Agent babysits a live run | trends + `tw watch` in the dispatcher; the `tune` role with a knob whitelist |
| Agent reads raw logs | `.tierwright/logs/` is blocked; use `tw brief JOB` |
| Grader or held-out set edited | `hooks.protected_paths` globs are blocked for Edit and Write |
| Same failure diagnosed again | rule table, plus `tw rule add --job INCIDENT` (backtested) |
| Dead end retried | the ledger's do-not-repeat list blocks the proposal |
| Everything on the top model | tiered roles, subagents pinned to haiku, sonnet or opus, and a builder per step tier |
| Silent spend | per-call, per-day and per-goal budgets, plus the owner queue |
| Context lost between sessions | `tw status` writes `HANDOFF.md` |

## Inside Claude Code

`tw init` installs slash commands in `.claude/commands/`. Each one runs its
`tw` command before the model reads anything, so the model sees only the
result, and each runs on the cheapest model that fits:

| Command | Does | Model |
|---|---|---|
| `/tw-status` | the project in at most 8 lines | Haiku |
| `/tw-step [notes]` | hand the step to the builder for its tier, which runs the gate and fixes up to twice | the step's tier |
| `/tw-check CHECK` | run one check | Haiku |
| `/tw-brief JOB` | explain a finished job from its brief | Haiku |
| `/tw-adopt [--no-model]` | plan from the codebase, summarize GAP.md | session model |
| `/tw-mine` | top mined proposals, installs nothing | Sonnet |
| `/tw-mine-apply ID` | install one proposal | Haiku |

It also installs the subagents `tw-status` (Haiku), `tw-ops` (Sonnet), and
`tw-builder-t1` / `-t2` / `-t3` (Haiku / Sonnet / Opus). Agent frontmatter fixes
one model per agent, so there is one builder per tier: `tw step show` prints
`# builder: tw-builder-t2 (sonnet, step tier T2)` first, and `/tw-step` hands
the step to that agent. `tw-builder` (Opus) stays as a fallback. Files you have
edited are never overwritten; `tw init --templates` adds missing ones to an
existing project.

## `tw spend`: model calls and agents

`tw spend` lists Tierwright's own model calls by role, tier and backend, then
Claude Code token use from this project's transcripts
(`~/.claude/projects/<project>/`), per day and agent type: main sessions as
`(main)`, subagents by the `agentType` in their `agent-*.meta.json`. Each
message's usage counts once, and cost is estimated at API rates from
`pricing:`, so the effect of moving watching and tuning into code shows up as
fewer agent runs. `--days N` (default 7), `--subagents-only`, `--no-agents`,
`--transcripts DIR...`. Outside a Tierwright project it reports the agents of
the current directory only.

## Commands

```
tw init [--lite] / adopt / plan / step show|list|check / done / check 'CHECK'
tw submit JOB [-- ARGS] / queue run [--watch] / queue stop NAME / dispatch / brief JOB
tw watch [JOB --until COND | JOB --clear]
tw contract [lint FILE...]   tw rule list|add|backtest   tw ask ROLE [FILE]
tw route [ROLE]   tw spend [--days N]   tw ledger [dnr]   tw status   tw init --templates
tw mine [apply ID]   tw tool [NAME]   tw digest PATH
```

A walk-through with no model calls is in `examples/hello/`.

## Project files

| Path | Committed | What |
|---|---|---|
| `tierwright.yaml` | yes | backend, budgets, tiers, role overrides, hooks, queue, contract dialect |
| `goal.yaml` | yes | the goal and its done bar |
| `steps/` | yes | the step ladder |
| `.tierwright/ledger.jsonl` | yes | every attempt and verdict |
| `.tierwright/rules.yaml`, `roles/` | yes | project triage rules, frozen role prompts |
| `.tierwright/tools/`, `tools.yaml`, `digests/` | yes | mined tools and digests |
| `GAP.md`, `STATUS.md`, `HANDOFF.md`, `MINE.md`, `owner-queue.md` | your call | written by code |
| `.tierwright/{queue,logs,briefs,runs,attention,trends,tune}`, `watches.json` | no | runtime state |

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
- Queue backends other than local and ssh (Slurm, Kubernetes).
- A builder that runs in a git worktree.
- Mined tools that take arguments, for commands that differ only by one
  value.
