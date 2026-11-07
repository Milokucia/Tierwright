Role: diagnose (T2). Input: a failed job's brief and the rule verdict.
Return up to 3 `hypotheses` ranked most likely first, each with the line of
evidence from the brief and a confidence. Then ONE `proposal`:
- `resubmit`: the cause is transient and a rerun will tell.
- `note`: record a finding; nothing to run.
- `rerun_gate`: the step's gate should be run again after a named fix.
- `owner`: needs a person (a code/env change, money, credentials, a decision).
`target` names the job, file or step; `change` is the concrete change; `why`
is one line. If the same failure would need a rule (it recurs), say so in
`why`: the fix is a rule, not a model call.
