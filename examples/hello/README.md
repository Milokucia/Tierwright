# Example: hello

A two-minute run through the loop, no model calls.

```bash
cd examples/hello
tw init --backend subscription --no-probe -y --goal "hello.sh prints hi" --done "bash hello.sh | grep -q hi"
tw adopt --no-model           # goal step first, then tests/CI hardening
tw submit build.job           # a job with an output contract
tw queue run                  # runs it; the dispatcher records "ok" with zero model calls
tw step check                 # the goal step's gate passes; the next step unlocks
tw done && tw status
```

Then break it on purpose: edit `build.job` to print `Traceback` and run the
queue again. The contract stops the job, and the dispatcher writes
`.tierwright/attention/build.json`. With `auto_diagnose` on, the diagnose
role (T2) proposes one action, and code applies it only if it is
whitelisted.
