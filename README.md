# HasbaraTops

This repository is the canonical governance and execution layer for HasbaraTops. Repository Markdown owns governance, strategy, and reusable evidence. One SQLite database outside Git owns Case and Turn state.

The runtime reads repository Markdown and the configured SQLite database only.

## Architecture

```text
AGENTS.md                         canonical governance
docs/reply-strategy-guide.md     canonical cross-case strategy
docs/evidence-base.md            canonical reusable evidence
docs/current-system-design.md    canonical implemented-system design
external SQLite database         canonical Cases and Turns
HasbaraTops CLI                  only canonical storage boundary
skills/                          model-driven analysis and reply workflows
```

`General responses` is protected and outside this workflow. The CLI exposes no operation for it.

## Deterministic boundary

Python and SQLite own URL parsing, identifiers, duplicate identity, schema constraints, lifecycle transitions, parent graphs, transactional writes, committed read-back, open-case summaries, strategy datasets, and backups.

The model owns interpretation, materially ambiguous parentage, reply drafting, fact-check judgment, and strategy analysis. Each canonical workflow ends in at most one high-level write command.

## Installation

```powershell
uv sync --frozen --extra dev
$env:HASBARATOPS_DB = '<outside-repo>\HasbaraTops.sqlite3'
uv run HasbaraTops db-init --approved
uv run HasbaraTops check
```

`HASBARATOPS_DB` must resolve outside the Git repository. `--database <path>` may override it and must appear before the subcommand.

Database initialization, backups, and Case or Turn mutations require `--approved`. Read commands do not mutate state.

## High-level commands

```text
HasbaraTops check
HasbaraTops db-init --approved
HasbaraTops db-status
HasbaraTops db-backup --destination <outside-repo-path> --approved

HasbaraTops case-find --case-id <Case-NNN>
HasbaraTops case-find --post-id <id> --root-comment-id <id>
HasbaraTops case-show --case-id <id>
HasbaraTops case-split-branch --case-id <id> --branch-root-turn-id <turn-id> --new-case-title <title> --new-topic <topic> --backup-destination <outside-repo-path> --approved
HasbaraTops case-list-open
HasbaraTops strategy-dataset

HasbaraTops case-intake <payload.json> --approved
HasbaraTops case-followup --case-id <id> <payload.json> --approved
HasbaraTops case-record-posting --case-id <id> <payload.json> --approved
HasbaraTops case-close --case-id <id> <payload.json> --approved
```

The high-level write commands allocate identifiers, validate all affected records, write inside an immediate SQLite transaction, commit, reopen, and compare the committed records. Errors produce compact JSON on stderr and a nonzero exit code. See the [current system design](docs/current-system-design.md) for command and payload contracts.

## Identity model

`case_id` is the definitive Case key. Case IDs use `Case-NNN` and are allocated from one global sequence; dates and Facebook identifiers are not part of the key. Separate reply branches may therefore be represented by multiple Cases with the same `post_id` and `root_comment_id`. `case-find --case-id` resolves one Case, while a root-based lookup returns every matching candidate for explicit selection.

A non-null `reply_comment_id` from a supplied permalink is globally unique across Turns. Without that value, the deterministic identity is `case_id + parent_turn_id + direction + exact_text`; `parent_turn_id` is null for a root Turn. Mutable ordering or a “latest reply” is never identity.

`case-list-open` returns one row per Case with the latest public Turn's supplied exact URL. It never substitutes the Case root URL; when the latest Turn has no supplied URL, it returns `permalink_status: "missing"` and a null permalink. This ordering is presentation only and never identity. When sibling branches in one Case must be tracked independently, `case-split-branch` keeps the selected branch in a newly allocated Case, copies its shared ancestor path with fresh case-local Turn IDs, creates a verified backup, commits transactionally, and reads both graphs back. It refuses to copy a shared ancestor carrying a globally unique `reply_comment_id`.

## Skills

| Skill | Purpose |
| --- | --- |
| `hasbaratops-intake` | Start or identify a case and prepare an approval-gated intake transaction. |
| `hasbaratops-followup` | Process a public turn in an existing case and prepare one follow-up transaction. |
| `hasbaratops-posting` | Record an explicitly confirmed published reply without posting to Facebook. |
| `hasbaratops-closeout` | Close a case from observable evidence through one approved transaction. |
| `hasbaratops-strategy-review` | Review closed-case evidence and propose strategy-guide changes. |

## HasbaraTops workflows

- Intake: `$hasbaratops-intake`
- Follow-up: `$hasbaratops-followup`
- Posting confirmation: `$hasbaratops-posting`
- Closeout: `$hasbaratops-closeout`
- Strategy review: `$hasbaratops-strategy-review`

Skills use read commands automatically. They may pass `--approved` only after the user approves the exact canonical write. No skill publishes to Facebook.

Bootstrap the managed Codex skills on a first installation:

```powershell
python scripts/install-skills-bootstrap.py --repo-root .
```

After the Ceratops skill lifecycle is installed, validate and refresh the
managed skills with:

```powershell
python scripts/install-skills.py --repo-root .
```

## Safety

- Never commit SQLite databases, journals, exports, backups, credentials, secrets, or public Facebook text.
- Never access Facebook unless explicitly requested; never post autonomously.
- Never overwrite a backup.
- Keep one canonical writer.

## Development

```powershell
uv sync --extra dev --frozen
uv run --no-sync python scripts/validate-repository.py `
  --evidence-file "$env:TEMP\HasbaraTops-validation.log"
```

The shared validator runs pytest, Ruff, mypy, database initialization, and the
authoritative readiness check against an isolated temporary SQLite database.
Tests use temporary SQLite databases and synthetic public text only.
