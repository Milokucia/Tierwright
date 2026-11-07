# Principles

Each principle came from a failure in a production agent pipeline (an RL
training system run by Claude Code agents around the clock). Each one is
enforced by code in Tierwright, not left to a prompt.

| # | Principle | Failure behind it | Enforced by |
|---|---|---|---|
| 1 | **Code first.** Anything with a rule runs as code. | A coordinator agent hand-queued every step of every chain. | `jobqueue`, `dispatch`, `rules` |
| 2 | **Event-driven, never polling.** | Agents slept and re-checked long jobs, burning context. | queue events; the `hooks` polling block |
| 3 | **Briefs, not logs.** A model gets about 2k tokens of JSON. | Diagnoses read whole logs and file trees; one cost 369k tokens. | `brief`; the raw-log hook |
| 4 | **Cheapest model that holds quality, per role, measured.** | Every role ran on the top model. | `config` tiers, `router`, `tw spend` |
| 5 | **Workers propose, code applies.** | Free-form agent edits raced each other and skipped checks. | `dispatch` whitelist, `owner-queue.md` |
| 6 | **Every job carries an output contract.** Verify outputs, not liveness. | A fine-tune hung 2.5 h on an interactive prompt while every liveness check passed. | `contract`; submit refuses a job without one |
| 7 | **Gates in cost order.** | Expensive checks ran before millisecond ones caught the typo. | schema check, then caller validation, then gate |
| 8 | **Judge by measurement, never the thing's own score.** | A reward that rose while the task got worse. | step gates and done bar are shell commands, not model opinions |
| 9 | **Graders are never generated into.** | Held-out sets drifted when tools could write to them. | `hooks.protected_paths`, plan validation |
| 10 | **A result in no ledger did not happen.** | The same dead end was paid for in three sessions. | `ledger`, do-not-repeat in every brief and plan |
| 11 | **Improve the search, not the answer.** | Hand-tuned fixes served one output and broke the next. | rules and steps over one-off patches |
| 12 | **Same failure diagnosed twice = a rule.** | Known failures kept waking a model. | `tw rule add --job`, with a backtest |
| 13 | **Hard budgets.** | Runs with no timeout, calls with no cap. | contract `timeout`, `limits`, `budgets` |
| 14 | **Stable prefixes cache.** | Role text was rebuilt per call and never cached. | frozen `roles/*.md` first; variable input after |
| 15 | **Escalate only money, secrets, deletes and outward actions.** | Owners were asked about every small env change. | `owner` proposals, owner queue |
| 16 | **Handoffs carry state.** | New sessions re-read history to find the current step. | `tw status` writes `HANDOFF.md` |
| 17 | **Start where the goal is.** | Infrastructure was built ahead of need and then unused. | the planner prompt; the T0 plan puts the goal step first |
