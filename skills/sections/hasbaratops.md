## Instruction enforcement

- Treat every instruction bullet in this skill as mandatory and closure-gating
  for the action it governs.
- Report the exact blocker instead of claiming completion when required
  approval, canonical evidence, or committed read-back is missing.

## Core rules

- Use the `HasbaraTops` CLI as the only Case and Turn storage boundary; do not
  use MCP for HasbaraTops work.
- Treat repository Markdown as canonical governance, strategy, and reusable
  evidence and the configured SQLite database as canonical Case and Turn state.
- Use only user-supplied public content and repository-owned canonical sources;
  never derive personal data from Facebook profiles.
- Never inspect or modify `General responses`. Interact with Facebook only when
  explicitly requested, and never publish autonomously.
- Before a canonical write, require approval for the exact transaction, run
  `HasbaraTops check`, execute one high-level write, and require committed
  read-back.

<!-- INTERNAL: include in every skill -->
