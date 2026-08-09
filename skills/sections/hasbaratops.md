## Instruction enforcement

- Treat every instruction bullet in this skill as mandatory and closure-gating
  for the action it governs.
- Report the exact blocker instead of claiming completion when required
  approval, canonical evidence, or committed read-back is missing.

## Core rules

- Use the `HasbaraTops` CLI as the only Case and Turn storage boundary; do not
  use MCP for HasbaraTops work.
- Resolve the CLI command once before the first operation. Use `HasbaraTops`
  when it is available on `PATH`. Otherwise, from an active HasbaraTops source
  checkout use `uv run --frozen HasbaraTops`; from an installed skill read
  `source_repository_root` in its `.runtime-manifest.json` and use
  `uv run --directory <source_repository_root> --frozen HasbaraTops`. Do not
  hard-code or search for the source repository path. Treat every
  `HasbaraTops ...` command below as this resolved prefix and stop if no route
  is executable.
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
