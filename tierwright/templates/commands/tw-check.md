---
description: Run one Tierwright check, e.g. /tw-check words: out/post.md 300-500
argument-hint: "file: PATH | words: PATH 300-500 | contains: PATH: a, b | review: PATH against GUIDE"
model: haiku
allowed-tools: Bash(tw check:*)
---
Run `tw check '$ARGUMENTS'` and report the result in at most 3 lines. If it failed, say what to change, quoting the check output.
