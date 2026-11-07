---
description: Explain a finished Tierwright job from its brief, not its logs
argument-hint: JOB
model: haiku
allowed-tools: Bash(tw brief:*)
---
!`tw brief $ARGUMENTS`

From the brief above only: did the job pass its contract, and if not, the most likely cause and the one next action, in at most 5 lines. Do not open logs.
