"""Run the repository validation contract in an isolated temporary workspace.

The caller owns environment setup and chooses both the temporary root and the
failure-evidence file. This runner never uses a configured canonical database:
every child process receives an explicit disposable database path instead.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_WORKSPACE_PREFIX = "hasbaratops-validation-"


class ValidationSetupError(RuntimeError):
    """Report an invalid or unsafe validation workspace configuration."""


@dataclass(frozen=True)
class Stage:
    """One fail-fast repository validation command."""

    name: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class StageResult:
    """Captured subprocess evidence for one validation stage."""

    stage: str
    command: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    launch_error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and self.launch_error is None


@dataclass(frozen=True)
class ValidationFailure:
    """Compact failure identity plus full file-backed diagnostics."""

    stage: str
    diagnostics: str
    cleanup_failed: bool = False


def _repository_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        raise ValidationSetupError("validation runner is not inside a Git checkout")
    return root


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validated_external_path(path: Path, repository: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if _is_within(resolved, repository):
        raise ValidationSetupError(f"{label} must be outside the repository")
    return resolved


def _validated_temp_root(path: Path, repository: Path) -> Path:
    root = _validated_external_path(path, repository, "temporary root")
    if not root.is_dir():
        raise ValidationSetupError(f"temporary root is not an existing directory: {root}")
    return root


def _create_workspace(temp_root: Path) -> Path:
    workspace = Path(tempfile.mkdtemp(prefix=_WORKSPACE_PREFIX, dir=temp_root)).resolve()
    if workspace.parent != temp_root or not workspace.name.startswith(_WORKSPACE_PREFIX):
        raise ValidationSetupError("temporary workspace escaped the selected root")
    return workspace


def _cleanup_workspace(workspace: Path, temp_root: Path) -> None:
    if not workspace.exists():
        return
    resolved = workspace.resolve()
    verified = (
        resolved.parent == temp_root
        and resolved.name.startswith(_WORKSPACE_PREFIX)
        and resolved.is_dir()
        and not workspace.is_symlink()
    )
    if not verified:
        raise ValidationSetupError("refusing to clean an unverified temporary workspace")
    shutil.rmtree(resolved)


def _stages(database: Path, workspace: Path) -> tuple[Stage, ...]:
    python = sys.executable
    return (
        Stage(
            "pytest",
            (
                python,
                "-m",
                "pytest",
                "-o",
                f"cache_dir={workspace / 'pytest-cache'}",
            ),
        ),
        Stage("ruff", (python, "-m", "ruff", "check", ".")),
        Stage(
            "mypy",
            (python, "-m", "mypy", "--cache-dir", str(workspace / "mypy-cache")),
        ),
        Stage(
            "db-init",
            (
                python,
                "-m",
                "hasbaratops.cli",
                "--database",
                str(database),
                "db-init",
                "--approved",
            ),
        ),
        Stage(
            "check",
            (
                python,
                "-m",
                "hasbaratops.cli",
                "--database",
                str(database),
                "check",
            ),
        ),
    )


def _isolated_environment(workspace: Path, database: Path) -> dict[str, str]:
    environment = os.environ.copy()
    temporary = str(workspace)
    environment.update(
        {
            "HASBARATOPS_DB": str(database),
            "MYPY_CACHE_DIR": str(workspace / "mypy-cache"),
            "PYTHONPYCACHEPREFIX": str(workspace / "pycache"),
            "RUFF_CACHE_DIR": str(workspace / "ruff-cache"),
            "TEMP": temporary,
            "TMP": temporary,
            "TMPDIR": temporary,
        }
    )
    return environment


def _run_stage(stage: Stage, repository: Path, environment: dict[str, str]) -> StageResult:
    try:
        completed = subprocess.run(
            list(stage.command),
            cwd=repository,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        return StageResult(
            stage=stage.name,
            command=stage.command,
            returncode=None,
            stdout="",
            stderr="",
            launch_error=f"{type(error).__name__}: {error}",
        )
    return StageResult(
        stage=stage.name,
        command=stage.command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _diagnostics(
    *,
    workspace: Path | None,
    database: Path | None,
    results: Sequence[StageResult],
    setup_error: str | None,
    cleanup_error: str | None,
) -> str:
    payload: dict[str, object] = {
        "database": str(database) if database is not None else None,
        "stages": [
            {
                "command": list(result.command),
                "launch_error": result.launch_error,
                "returncode": result.returncode,
                "stage": result.stage,
                "stderr": result.stderr,
                "stdout": result.stdout,
            }
            for result in results
        ],
        "temporary_workspace": str(workspace) if workspace is not None else None,
    }
    if setup_error is not None:
        payload["setup_error"] = setup_error
    if cleanup_error is not None:
        payload["cleanup_error"] = cleanup_error
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def run_validation(temp_root: Path) -> ValidationFailure | None:
    """Run all stages in order, stopping at the first failure and always cleaning."""

    repository: Path | None = None
    root: Path | None = None
    workspace: Path | None = None
    database: Path | None = None
    results: list[StageResult] = []
    failed_stage: str | None = None
    setup_error: str | None = None
    cleanup_error: str | None = None

    try:
        repository = _repository_root()
        root = _validated_temp_root(temp_root, repository)
        workspace = _create_workspace(root)
        database = workspace / "HasbaraTops-validation.sqlite3"
        environment = _isolated_environment(workspace, database)
        for stage in _stages(database, workspace):
            result = _run_stage(stage, repository, environment)
            results.append(result)
            if not result.succeeded:
                failed_stage = stage.name
                break
    except (OSError, ValidationSetupError) as error:
        failed_stage = "preflight"
        setup_error = f"{type(error).__name__}: {error}"
    finally:
        if workspace is not None and root is not None:
            try:
                _cleanup_workspace(workspace, root)
            except (OSError, ValidationSetupError) as error:
                cleanup_error = f"{type(error).__name__}: {error}"
                if failed_stage is None:
                    failed_stage = "cleanup"

    if failed_stage is None:
        return None
    return ValidationFailure(
        stage=failed_stage,
        diagnostics=_diagnostics(
            workspace=workspace,
            database=database,
            results=results,
            setup_error=setup_error,
            cleanup_error=cleanup_error,
        ),
        cleanup_failed=cleanup_error is not None,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate the HasbaraTops repository")
    parser.add_argument("--temp-root", required=True, type=Path)
    parser.add_argument("--evidence-file", required=True, type=Path)
    return parser


def _emit_failure(failure: ValidationFailure, evidence_file: Path) -> int:
    payload: dict[str, object] = {
        "evidence_file": str(evidence_file),
        "ok": False,
        "stage": failure.stage,
    }
    if failure.cleanup_failed:
        payload["cleanup_failed"] = True
    try:
        evidence_file.parent.mkdir(parents=True, exist_ok=True)
        evidence_file.write_text(failure.diagnostics, encoding="utf-8")
    except OSError as error:
        payload["evidence_error"] = f"{type(error).__name__}: {error}"
    print(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        file=sys.stderr,
    )
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    evidence_file = Path(args.evidence_file).expanduser().resolve()
    failure = run_validation(Path(args.temp_root))
    if failure is not None:
        return _emit_failure(failure, evidence_file)
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
