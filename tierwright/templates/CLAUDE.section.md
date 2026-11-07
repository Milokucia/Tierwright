## Tierwright (read first)

This project runs on Tierwright: code does routine work, agents do judgment, every output is checked.

- **Be short.** Answer, then stop. Numbers, not adjectives.
- **Start with `tw status`** (<= 20 lines). Never read `.tierwright/logs/`: use `tw brief JOB`.
- **Work the ladder.** `tw step show` is the only step to build. `tw step check` runs its gate; a step is done only when its gate passes.
- **Never poll.** Long work is a job with an output contract: `tw submit FILE`, then end your turn. The dispatcher acts on its end.
- **Read the do-not-repeat list** (`tw ledger dnr`) before proposing anything.
- **Same failure diagnosed twice = a rule** (`tw rule add`), not a third diagnosis.
- **Use mined tools first:** `tw tool` lists one-call replacements for command sequences agents repeat; `tw mine` finds new ones.
- **Pick the cheapest agent:** `tw-status` (haiku) for questions, `tw-ops` (sonnet) to run things, `tw-builder` (opus) to build a step.
- **Owner only:** money, secrets, deletes, push/publish/launch. Write them to `owner-queue.md`.
