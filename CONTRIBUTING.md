# Contributing

Open an issue before substantial changes. Keep changes narrowly scoped, preserve
the local SQLite transaction boundary, and never commit databases, exports,
backups, credentials, secrets, or public Facebook content.

Set up the locked development environment, then run the shared validation
entrypoint. It creates and removes its own disposable database below the
selected temporary root; it does not use `HASBARATOPS_DB`.

```powershell
uv sync --extra dev --frozen
uv run --no-sync python scripts/validate_repository.py `
  --temp-root "$env:TEMP" `
  --evidence-file "$env:TEMP\HasbaraTops-validation.json"
```

Contributions are accepted only under the repository's proprietary license.
