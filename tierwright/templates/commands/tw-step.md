---
description: Build the current Tierwright step and run its gate
allowed-tools: Bash(tw step:*), Bash(tw check:*), Bash(tw status:*)
---
!`tw step show`

Build exactly the step above and nothing else.

1. Make the listed `changes`. $ARGUMENTS
2. Run `tw step check`.
3. If a check fails, fix what it reports and run `tw step check` again. Stop after 2 fixes and report the failing check.
4. Do not start the next step. Do not push or publish.

Report in at most 6 lines: the step id, the files changed, and the gate result.
