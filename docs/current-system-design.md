# HasbaraTops Current System Design

## 1. Scope and status

This document describes the functionality implemented in the repository now. It
is the canonical implemented-system design for the HasbaraTops CLI, deterministic
domain logic, SQLite storage, and installed workflow skills.

The Chrome extension is not implemented. Its approved architecture is described
separately in `docs/chrome-extension-design.md`. Temporary implementation
sequences and task prompts are not repository design.

## 2. System purpose

HasbaraTops manages structured public-discussion Cases and Turns while keeping
model judgment separate from deterministic identity, validation, lifecycle, and
storage behavior.

The system has two canonical content surfaces:

- Repository Markdown owns governance, reply strategy, reusable evidence, and
  system design.
- One configured SQLite database outside Git owns Case and Turn state.

The `HasbaraTops` CLI is the only canonical storage boundary. Skills may prepare
and invoke CLI operations, but neither skills nor models write SQLite directly.

## 3. Architecture

```text
AGENTS.md
  -> project governance and safety

docs/reply-strategy-guide.md
docs/evidence-base.md
  -> strategy and reusable evidence

skills/hasbaratops-*
  -> model-driven intake, follow-up, posting, closeout, and strategy workflows

HasbaraTops CLI
  -> deterministic parsing, validation, identity, lifecycle, reads, and writes

src/hasbaratops/storage.py
  -> transactional SQLite implementation

external configured SQLite database
  -> canonical Cases and Turns
```

`General responses` is outside this architecture. The CLI has no operation for
it.

## 4. Runtime configuration

`config/storage.toml` defines:

- Project name and timezone.
- SQLite schema version.
- The environment variable that supplies the database path.
- Canonical governance, strategy, and evidence document paths.

The database path comes from `HASBARATOPS_DB` or the CLI-level
`--database <path>` override. The resolved database must be outside the Git
repository.

The runtime is Python 3.12 or newer. The installable command is:

```text
HasbaraTops
```

Operational command results are compact JSON on standard output. Operational
failures produce compact JSON on standard error and a nonzero exit code.

## 5. Domain model

### 5.1 Case

A Case contains:

- `case_id`
- `case_title`
- `created_at`
- `updated_at`
- `status`
- `topic`
- `post_text`
- `post_url`
- `post_id`
- `root_comment_id`
- `source_links`
- `privacy_checked`
- Outcome and review fields used at closure

`case_id` uses the global positive `Case-NNN` sequence. The CLI allocates the next
identifier during intake.

Required Case content includes title, timestamps, topic, exact post text, exact
post URL, Post ID, Root Comment ID, and a true privacy check. Open Cases require
a post URL containing a comment or reply identifier.

### 5.2 Turn

A Turn contains:

- `case_id`
- `turn_id`
- `parent_turn_id`
- `parent_confidence`
- `participant_ref`
- `direction`
- `kind`
- `state`
- `exact_text`
- `post_id`
- `root_comment_id`
- `reply_comment_id`
- `exact_url`
- `url_supplied_at`
- `observed_at`
- `notes`

`turn_id` uses a Case-local positive `TNNN` sequence. Participant references are
`USER` or Case-local `P1`, `P2`, and later positive P-numbers. Profile identity
is not part of the model.

Exact text is required and remains canonical. Incoming Turns cannot be Drafts.
When an exact URL is present, its parsed identifiers must agree with the Turn.

### 5.3 Enumerated state

Case statuses:

- `Draft`
- `Posted`
- `Active Exchange`
- `Closed - No Response`
- `Closed - Disengaged`
- `Closed - Substantive`
- `Closed - Claim Narrowed`
- `Closed - Correction`
- `Closed - Abandoned`

Turn directions are `Incoming` and `Outgoing`.

Turn kinds are `Comment`, `Reply`, and `Reaction`.

Turn states are `Received`, `Draft`, `Answered`, `Posted`, `Replaced`, and
`Ignored`.

Parent confidence values are `User-confirmed`, `URL-derived`, `Screenshot`,
`Inferred`, and `Unknown`.

Outcome classes are `No Response`, `Insult/Repetition`,
`Substantive Engagement`, `Source Exchange`, `Claim Narrowed`,
`Uncertainty Acknowledged`, `Explicit Correction`, `Mixed`, and `Abandoned`.

## 6. Identity

### 6.1 Case identity

Case ID is definitive. Post ID plus Root Comment ID is a candidate-discovery key,
not Case identity. Multiple Cases may intentionally share one Facebook root when
they track different branches.

`case-find --case-id` resolves one Case. Root lookup returns every candidate and
never silently selects or reuses one.

### 6.2 Turn identity

Turn duplicate resolution uses:

1. Supplied non-null `reply_comment_id`, globally unique across Turns.
2. Otherwise, Case ID + Parent Turn ID + Direction + Exact Text.

A root Turn participates in the fallback identity with a null Parent Turn ID.
Timestamps, ordering, current status, and the latest reply never determine
identity.

### 6.3 URL parsing

`HasbaraTops parse-url` preserves the exact supplied URL and deterministically
extracts:

- Facebook-host validity.
- Post ID from supported post, reel, photo, or `fbid` forms.
- Root Comment ID from `comment_id`.
- Reply Comment ID from `reply_comment_id`.
- Conflicting or missing identifier errors.

Parsing does not rewrite the exact URL. URL parsing does not determine direct
Turn parentage.

## 7. Lifecycle

Allowed Case transitions:

```text
Draft -> Posted
Posted -> Active Exchange
Posted -> any Closed status
Active Exchange -> any Closed status
Closed status -> no later status
```

A same-status transition is allowed. Every changed transition requires a
non-empty reason.

Posting confirmation requires an Outgoing Turn in Posted state with the exact
published wording.

Closure requires a Closed status, updated and closed timestamps, outcome score,
outcome class, outcome notes, what worked, what failed, and next test. Silence,
deletion, blocking, a reaction, or disappearance alone cannot establish
persuasion.

## 8. SQLite design

Schema version is `1`. The database contains:

- `storage_metadata`
- `cases`
- `turns`

Important constraints:

- Canonical positive `Case-NNN` Case IDs.
- Case-local composite Turn primary key.
- Turn-to-Case and parent-Turn foreign keys.
- Global uniqueness for non-null `reply_comment_id`.
- Fallback Turn-identity uniqueness when `reply_comment_id` is absent.
- A non-unique Case root-candidate index.
- Enum, score, rating, and privacy checks.

Initialization refuses an unrelated non-empty unversioned database. Status
validation checks schema version, schema signature, table columns, indexes,
foreign keys, integrity, and row counts.

SQLite uses one canonical writer. Every canonical mutation:

1. Requires explicit approval.
2. Validates records and the complete affected parent graph.
3. Starts an immediate transaction.
4. Applies the complete high-level operation.
5. Commits.
6. Reopens and verifies the committed state.

A failed write must leave rollback and database integrity verified before another
write.

## 9. CLI surface

### 9.1 Readiness and storage

| Command | Behavior |
| --- | --- |
| `check` | Verifies configured Markdown, schema version, database shape, integrity, and document revisions. |
| `db-init --approved` | Initializes an empty outside-Git database or verifies the existing canonical database. |
| `db-status` | Returns schema, signature, integrity, Case count, and Turn count. |
| `db-backup --destination <path> --approved` | Creates and verifies one non-overwriting outside-Git SQLite backup. |

### 9.2 Pure deterministic helpers

| Command | Behavior |
| --- | --- |
| `parse-url <url>` | Parses supported Facebook identifiers without rewriting the URL. |
| `next-case-id --existing <json>` | Calculates the next global Case ID. |
| `next-turn-id --case-id <id> --existing <json>` | Calculates the next Case-local Turn ID. |
| `validate-case <json>` | Validates one complete Case record. |
| `validate-turn <json>` | Validates one complete Turn record. |
| `validate-transition <json>` | Validates one lifecycle transition. |
| `validate-parent-graph <json>` | Validates one Case-local Turn graph. |
| `verify-readback --expected <json> --actual <json>` | Compares expected and actual fields. |

### 9.3 Case reads

| Command | Behavior |
| --- | --- |
| `case-find --case-id <id>` | Resolves the definitive Case ID. |
| `case-find --post-id <id> --root-comment-id <id>` | Returns every root candidate. |
| `case-show --case-id <id>` | Returns one Case and its complete Turn graph. |
| `case-list-open` | Returns each open Case with its latest public Turn's supplied exact URL. |
| `strategy-dataset` | Returns all closed Cases and their Turns. |

`case-list-open` excludes Draft and Replaced Turns from latest-public-Turn
selection. A missing exact URL is returned as null with
`permalink_status: "missing"`; the Case root URL is never substituted.

### 9.4 Canonical workflows

| Command | Behavior |
| --- | --- |
| `case-intake <json> --approved` | Allocates one Case ID and Case-local Turn IDs, then creates the Case and initial graph atomically. |
| `case-followup --case-id <id> <json> --approved` | Records one Incoming Turn and moves an open Case to Active Exchange. |
| `case-record-posting --case-id <id> <json> --approved` | Records one confirmed Outgoing Posted Turn and may replace one stored Draft. |
| `case-close --case-id <id> <json> --approved` | Applies observable closure fields and one Closed status atomically. |
| `case-split-branch ... --approved` | Moves one existing reply branch into the next global Case with a verified backup and committed read-back. |

Closed Cases reject follow-up and posting operations.

## 10. Payload contracts

All payloads are UTF-8 JSON with lowercase `snake_case` fields. Unknown fields
are rejected.

### 10.1 Intake

`case-intake` accepts:

```json
{
  "case": {
    "case_title": "Short title",
    "created_at": "YYYY-MM-DD HH:MM",
    "updated_at": "YYYY-MM-DD HH:MM",
    "status": "Posted",
    "topic": "Topic",
    "post_text": "Exact public post text",
    "post_url": "Exact supplied Facebook permalink",
    "post_id": "Post ID",
    "root_comment_id": "Root Comment ID",
    "source_links": [],
    "privacy_checked": true
  },
  "turns": []
}
```

The Case and initial Turns omit allocated `case_id` and `turn_id` values. Turn
Post ID and Root Comment ID are derived from the Case.

### 10.2 Follow-up and posting

`case-followup` and `case-record-posting` accept one Turn payload without
`case_id`, `turn_id`, `post_id`, or `root_comment_id`:

```json
{
  "parent_turn_id": "T001",
  "parent_confidence": "User-confirmed",
  "participant_ref": "P1",
  "direction": "Incoming",
  "kind": "Reply",
  "state": "Received",
  "exact_text": "Exact public text",
  "reply_comment_id": "Reply Comment ID or null",
  "exact_url": "Exact supplied permalink or null",
  "url_supplied_at": "YYYY-MM-DD HH:MM or null",
  "observed_at": "YYYY-MM-DD HH:MM",
  "notes": ""
}
```

Follow-up requires `direction: "Incoming"`.

Posting requires `direction: "Outgoing"` and `state: "Posted"`. It may include
`draft_turn_id` to mark one stored Outgoing Draft as Replaced in the same
transaction.

When an exact URL supplies a Reply Comment ID, the CLI adopts it unless it
conflicts with an explicitly supplied value.

### 10.3 Closeout

`case-close` accepts:

```json
{
  "status": "Closed - Substantive",
  "updated_at": "YYYY-MM-DD HH:MM",
  "outcome_score": 3,
  "outcome_class": "Substantive Engagement",
  "outcome_notes": "Observable outcome",
  "user_rating": null,
  "what_worked": "Observation",
  "what_failed": "Observation",
  "next_test": "Controlled next test",
  "closed_at": "YYYY-MM-DD HH:MM",
  "reason": "Explicit closeout"
}
```

Only closure fields change. Case identity and public context remain unchanged.

### 10.4 Branch split

`case-split-branch` receives its values as CLI arguments:

```text
--case-id <id>
--branch-root-turn-id <turn-id>
--new-case-title <title>
--new-topic <topic>
--backup-destination <outside-repo-path>
--approved
```

The selected branch root must be a non-root Turn while another branch remains in
the source Case. The operation:

- Creates and verifies a non-overwriting backup.
- Copies the shared ancestor path with fresh Case-local Turn IDs.
- Moves the selected branch and descendants.
- Preserves exact public text and URLs.
- Validates both resulting graphs.
- Commits and reads both Cases back.

The operation refuses to copy a shared ancestor carrying a globally unique Reply
Comment ID.

## 11. Skill boundary

Repository-root `skills/` is the authoritative HasbaraTops skill source:

- `hasbaratops-intake`
- `hasbaratops-followup`
- `hasbaratops-posting`
- `hasbaratops-closeout`
- `hasbaratops-strategy-review`

`skills/skill-sections.json` is the runtime manifest. Every skill receives the
canonical Ceratops-compatible rules from `skills/sections/core.md` and the
HasbaraTops-specific shared rules from `skills/sections/hasbaratops.md`; source
`SKILL.md` files remain delta-only. `scripts/install-skills-bootstrap.py` owns
first installation, while `scripts/install-skills.py` validates and refreshes
skills through an installed lifecycle bundle. `deploy/deploy.yml` declares the
bootstrap operation and the managed lifecycle deployment handoff.

The model owns interpretation, ambiguous parentage judgment, reply drafting,
fact-check judgment, and strategy analysis. Deterministic helpers own identifiers,
duplicates, graphs, payload validation, lifecycle transitions, and persistence.

Each Case workflow prepares at most one high-level canonical write. A skill may
pass `--approved` only after explicit approval of the exact write. No skill posts
to Facebook.

## 12. Safety boundaries

- Never access Facebook unless the user explicitly requests that interaction.
- Never post to Facebook autonomously.
- Never inspect or modify `General responses` without exact authorization.
- Never commit SQLite databases, journals, backups, exports, credentials,
  secrets, or public Facebook text.
- Keep databases and backups outside the repository.
- Never overwrite a backup.
- Keep one canonical writer.
- Treat a failed write as blocking until rollback and integrity are verified.

## 13. Repository validation

`scripts/validate_repository.py` is the shared local-and-CI validation
entrypoint. Environment creation remains a caller responsibility. After
`uv sync --extra dev --frozen`, the runner executes, in fail-fast order:

1. pytest
2. Ruff
3. mypy
4. `db-init --approved`
5. `check`

The runner resolves the repository checkout, requires a caller-selected
temporary root outside it, creates one uniquely named child workspace, and
overrides `HASBARATOPS_DB` for every child process. Initialization and readiness
also receive the same explicit database argument. Tool caches, Python bytecode,
and the disposable SQLite database stay within that verified workspace. Cleanup
removes only that child and never removes the selected root. Before a run, the
runner removes only the exact caller-selected evidence file, if present, so an
`OK` result cannot coexist with stale failure diagnostics.

Successful validation emits only `OK` and leaves no evidence file. Failure emits
one compact JSON object with the failed stage and caller-selected evidence path;
that file contains the full captured command, exit status, stdout, and stderr
for each attempted stage. CI emits that file in a failure-only step so the full
diagnostics remain available in the job log.

The former Drive-era `doctor` command checked Git-checkout presence, the
governance bootstrap, Drive configuration, and a Drive schema signature. The
SQLite redesign removed that command and introduced `check`. The runner now
owns the Git-checkout preflight, while `check` covers the still-applicable and
replacement invariants: configured governance, strategy, and evidence files;
storage configuration; configured schema version; database shape and integrity;
and document revisions. Drive-only invariants no longer exist. Therefore
`check` is the authoritative database-health command and `doctor` is not
retained or reintroduced.

## 14. Design maintenance

Every implementation, interface, workflow, or governing-behavior change must
update each affected canonical design document in the same change. Completion is
blocked when design and current behavior differ.
