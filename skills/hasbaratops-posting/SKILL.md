---
name: hasbaratops-posting
description: Record a user's explicit confirmation that a HasbaraTops reply was posted, using one deterministic SQLite transaction; this skill never publishes to Facebook.
---

# HasbaraTops Posting Confirmation

## Required inputs

- Target Case ID and parent Turn ID.
- Exact wording actually published, even when it differs from the recommendation.
- Supplied permalink, identifiers, posting time, and optional Draft Turn ID.

## Workflow

1. Load the Case once with `HasbaraTops case-show`.
2. Parse any supplied URL with `HasbaraTops parse-url`; preserve it exactly. Use supplied `reply_comment_id` as the strongest duplicate identity, but resolve the immediate parent only from visible context or user confirmation.
3. Prepare one Outgoing Posted Turn payload containing the exact published wording. Include `draft_turn_id` only when an existing Outgoing Draft must be marked Replaced.
4. After explicit approval, run `HasbaraTops check`; if it passes, run exactly one `HasbaraTops case-record-posting --case-id <id> <payload> --approved` transaction. Without `reply_comment_id`, it deduplicates by Case ID + Parent Turn ID (including a null root) + Direction + Exact Text. It otherwise allocates the Turn ID, validates the graph and lifecycle, writes atomically, and reads back the committed state.

## Safety

- Never publish, edit, or delete Facebook content.
- Never assume the posted text equals an earlier draft.

## Output

Return only the compact command receipt plus exact-text and permalink preservation status.
