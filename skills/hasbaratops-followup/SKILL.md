---
name: hasbaratops-followup
description: Process supplied public content in an existing HasbaraTops case, resolve parentage, and prepare one approval-gated SQLite follow-up transaction.
---

# HasbaraTops Follow-up

## Required inputs

- Existing Case ID or enough Post ID + Root Comment ID and branch context to select one candidate.
- Exact new public turn or reaction, supplied URL, observed time, and visible parent context.

## Workflow

1. Treat an explicit Case ID as definitive; otherwise use root-based `HasbaraTops case-find` only for candidate discovery and select the Case from branch context, asking when multiple candidates remain materially ambiguous. Load the selected Case once with `HasbaraTops case-show`.
2. Parse supplied URLs with `HasbaraTops parse-url`, preserve each exact URL, and confirm that the selected Case contains the tracked branch. Never identify a Case from its latest reply.
3. When sibling branches already stored in one Case must be tracked independently, prepare one `HasbaraTops case-split-branch` command with an outside-Git backup and exact new title/topic. After explicit approval, run `HasbaraTops check`; if it passes, run that command and use its committed Case/Turn mapping.
4. Assign or reuse only `P1`, `P2`, ... or `USER`. Resolve the direct parent from visible context or user confirmation and retain Parent Confidence; ask only when ambiguity changes the reply.
5. Build one incoming Turn payload. A supplied `reply_comment_id` is its strongest duplicate identity; without one, the transaction uses Case ID + Parent Turn ID (including a null root) + Direction + Exact Text. The transaction returns the existing Turn on a duplicate or allocates a Turn ID, validates the complete parent graph, appends, commits, and reads back.
6. Read repository strategy and evidence Markdown only as needed and verify material claims.
7. Base the public reply's substantive response only on the supplied public post, target comment chain, and verified relevant facts; never treat private planning, predicted replies, or unrelated conversation context as public content.
8. Return the standard output. After explicit approval, run `HasbaraTops check`; if it passes, run exactly one `HasbaraTops case-followup --case-id <id> <payload> --approved` transaction and report its compact receipt.

## Output

Return, in order: `Case ID / Status`, optional `Thread Map`, `Claim Map`, `Recommended Reply`, `Shorter Version`, `Why This Approach`, `Fact Check`, `Conversational Potential`, `Log Tags`, and `Next Action`.

Use the Facebook thread's language. Ignore incidental abuse; use `no_engagement` for credible threats, doxxing, calls for violence, spam, or no testable claim.
