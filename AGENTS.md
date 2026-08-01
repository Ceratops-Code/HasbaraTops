# HasbaraTops

## Project rules

- [HASBARA-CHECK-01] Run `HasbaraTops check` before a canonical write,
  after a failed write or readiness check, or when database state is uncertain;
  ordinary read-only queries must reuse fresh sufficient evidence.
- [HASBARA-CANONICAL-01] Treat repository Markdown as canonical governance,
  strategy, and evidence content and the configured SQLite database as canonical
  Case and Turn state; do not use MCP for HasbaraTops work.
- [HASBARA-IDENTITY-01] Use `config/storage.toml`, preserve the SQLite schema
  version, treat Case ID as definitive, allow multiple Cases per Facebook root,
  and use root lookup only for candidate discovery.
- [HASBARA-IDENTITY-02] Deduplicate Turns by supplied `reply_comment_id`; when
  absent, use Case ID, Parent Turn ID, Direction, and Exact Text; never use
  mutable latest-reply state as identity.
  - limits: HASBARA-OPEN-CASES-01
- [HASBARA-OPEN-CASES-01] Present each open Case with its latest public Turn's
  supplied exact URL; never substitute the Case root URL, and mark a missing
  link explicitly.
- [HASBARA-SKILL-ROUTING-01] Invoke the matching HasbaraTops skill for the
  requested case or strategy workflow.
- [HASBARATOPS-REPLY-01] When proposing a public HasbaraTops reply, provide one
  complete, self-contained, ready-to-post response.
- [HASBARA-GENERAL-RESPONSES-01] Never inspect, edit, move, rename, replace,
  delete, import, or summarize `General responses` without explicit instruction
  for that exact action.
- [HASBARA-WRITE-01] Perform canonical writes only through explicitly approved
  `HasbaraTops` commands using SQLite transactions and committed read-back.
- [HASBARA-ROLLBACK-01] After a failed canonical write, verify rollback and
  database integrity; block further writes until both pass.
- [HASBARA-FACEBOOK-01] Interact with Facebook only when the user explicitly
  requests that interaction; never post autonomously.
- [HASBARA-SENSITIVE-STATE-01] Never commit SQLite state, data exports or
  backups, or credentials and secrets.
- [HASBARA-DESIGN-01] Update every affected canonical design document in the
  same change as any implementation, interface, workflow, or governing-behavior
  change; block completion when design and current behavior differ.
- [HASBARA-SKILLS-SOURCE-01] Treat repo-root `skills/` as the authoritative
  source for HasbaraTops skills.
- [HASBARA-SKILLS-LIFECYCLE-01] Use the installed Ceratops skill lifecycle for
  skill maintenance, runtime installation, and release staging through the
  repository installer.
