## Summary

-

## Validation

- [ ] `uv sync --extra dev --frozen`
- [ ] `uv run --no-sync python scripts/validate_repository.py --temp-root "$env:TEMP" --evidence-file "$env:TEMP\HasbaraTops-validation.json"`

## Repository contracts

- [ ] Affected canonical design documents are updated with behavior changes.
- [ ] No SQLite state, data export, backup, credential, secret, or private case data is committed.
