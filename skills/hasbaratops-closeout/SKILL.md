---
name: hasbaratops-closeout
description: Close a HasbaraTops case from observable evidence and record the result through one approval-gated SQLite transaction.
---

# HasbaraTops Closeout

## Required inputs

- Target Case ID and allowed closure condition.
- Any final public turn, reaction, or user report needed to establish the observable ending.

## Workflow

1. Load the complete Case and Turn graph once with `HasbaraTops case-show`.
2. Resolve any missing public Turn through `$hasbaratops-followup` before closure.
3. Verify privacy and record observable chronology only. Never infer persuasion from silence, deletion, blocking, a reaction, or disappearance.
4. Choose the closed status, Outcome Class, highest outcome score reached, concise Outcome Notes, What Worked, What Failed, and exactly one Next Test.
5. Prepare one closeout payload. After explicit approval, run `HasbaraTops check`; if it passes, run exactly one `HasbaraTops case-close --case-id <id> <payload> --approved` transaction and report its compact receipt.

## Safety

- Do not close solely because no new message is visible unless the user requests or reports no-response closure.

## Output

Return Case ID, final status, Outcome Score, Outcome Class, observable basis, What Worked, What Failed, Next Test, and read-back status.
