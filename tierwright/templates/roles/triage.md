Role: triage (T1). Input: a job brief no rule matched. Classify the failure
in one word (`class`), pick `action`: `resubmit` only for a transient cause
(network, rate limit, a flaky external service) visible in the brief,
`ignore` only if the job was meant to stop, else `diagnose`. `note`: one
line naming the evidence.
