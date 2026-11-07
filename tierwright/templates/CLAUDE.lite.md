## Tierwright (read first)

This project runs on Tierwright lite: the cheapest model that can do each task, a budget, and a check that proves each step done.

- **Be short.** Answer, then stop.
- **Start with `tw status`.** Work only on the step `tw step show` prints.
- **A step is done when `tw step check` passes.** Its checks are plain language: `file:`, `words:`, `contains:`, `lacks:`, `review: FILE against GUIDE`. Fix what a failed check says, then check again.
- **Pick the cheapest agent:** `tw-status` (haiku) for questions, `tw-ops` (sonnet) for routine drafts and edits, `tw-builder` (opus) for strategy or a hard step.
- **Use mined tools first:** `tw tool` lists them; `tw mine` finds new ones.
- **Never edit** the files listed under `protected_paths` in `tierwright.yaml` (guides, approved copy).
- **Ask the owner** before spending money, sending, posting or publishing anything. Write it in `owner-queue.md`.
