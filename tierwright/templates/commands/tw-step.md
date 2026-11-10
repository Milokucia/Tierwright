---
description: Build the current Tierwright step and run its gate
allowed-tools: Bash(tw step:*), Bash(tw check:*), Bash(tw status:*), Agent
---
!`tw step show`

The first line above names the builder subagent for this step's tier
(`tw-builder-t1` haiku, `-t2` sonnet, `-t3` opus).

1. Hand the step to that subagent with the Agent tool (subagent_type = the
   name on the `# builder:` line). Pass it these notes: $ARGUMENTS
2. If that subagent is not installed (`tw init --templates` adds it), build the
   step yourself: make the listed `changes`, run `tw step check`, fix up to 2
   times.
3. Do not start the next step. Do not push or publish.

Report in at most 6 lines: the step id, the builder used, the files changed,
and the gate result.
