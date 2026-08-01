import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from scripts import validate_repository

RunCall = tuple[list[str], dict[str, object]]


def test_validation_order_isolation_cleanup_and_compact_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    temp_root = tmp_path / "temporary root with spaces"
    temp_root.mkdir()
    sentinel = temp_root / "caller-owned.txt"
    sentinel.write_text("keep", encoding="utf-8")
    real_database = tmp_path / "user database.sqlite3"
    real_database.write_text("untouched", encoding="utf-8")
    evidence_file = tmp_path / "evidence files" / "validation.json"
    evidence_file.parent.mkdir()
    evidence_file.write_text('{"stale":true}\n', encoding="utf-8")
    monkeypatch.setenv("HASBARATOPS_DB", str(real_database))
    calls: list[RunCall] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="passed\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = validate_repository.main(
        [
            "--temp-root",
            str(temp_root),
            "--evidence-file",
            str(evidence_file),
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == "OK\n"
    assert captured.err == ""
    assert not evidence_file.exists()
    assert [command[2] for command, _ in calls] == [
        "pytest",
        "ruff",
        "mypy",
        "hasbaratops.cli",
        "hasbaratops.cli",
    ]
    assert calls[0][0][3:5] == ["-o", f"cache_dir={calls[0][0][4].split('=', 1)[1]}"]
    assert calls[1][0][3:] == ["check", "."]
    assert calls[2][0][3] == "--cache-dir"
    assert calls[3][0][-2:] == ["db-init", "--approved"]
    assert calls[4][0][-1] == "check"

    database_arguments: list[Path] = []
    for command, kwargs in calls:
        assert command[:2] == [sys.executable, "-m"]
        environment = cast(dict[str, str], kwargs["env"])
        isolated_database = Path(environment["HASBARATOPS_DB"])
        assert isolated_database != real_database
        assert isolated_database.parent.parent == temp_root.resolve()
        assert " " in str(isolated_database)
        assert environment["TEMP"] == str(isolated_database.parent)
        assert environment["TMP"] == str(isolated_database.parent)
        assert environment["TMPDIR"] == str(isolated_database.parent)
        if "--database" in command:
            database_arguments.append(Path(command[command.index("--database") + 1]))
    assert database_arguments == [
        Path(cast(dict[str, str], calls[0][1]["env"])["HASBARATOPS_DB"]),
        Path(cast(dict[str, str], calls[0][1]["env"])["HASBARATOPS_DB"]),
    ]
    assert real_database.read_text(encoding="utf-8") == "untouched"
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert list(temp_root.iterdir()) == [sentinel]


def test_validation_fails_fast_with_compact_json_and_full_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    temp_root = tmp_path / "temporary root with spaces"
    temp_root.mkdir()
    sentinel = temp_root / "caller-owned.txt"
    sentinel.write_text("keep", encoding="utf-8")
    evidence_file = tmp_path / "evidence files" / "failure details.json"
    calls: list[RunCall] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if command[2] == "ruff":
            return subprocess.CompletedProcess(
                command,
                7,
                stdout="ruff stdout\n",
                stderr="ruff failed in full detail\n",
            )
        return subprocess.CompletedProcess(command, 0, stdout="pytest passed\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = validate_repository.main(
        [
            "--temp-root",
            str(temp_root),
            "--evidence-file",
            str(evidence_file),
        ]
    )

    captured = capsys.readouterr()
    expected = {
        "evidence_file": str(evidence_file.resolve()),
        "ok": False,
        "stage": "ruff",
    }
    assert result == 1
    assert captured.out == ""
    assert captured.err == (
        json.dumps(expected, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    )
    assert len(captured.err.splitlines()) == 1
    assert [command[2] for command, _ in calls] == ["pytest", "ruff"]

    diagnostics = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert [stage["stage"] for stage in diagnostics["stages"]] == ["pytest", "ruff"]
    assert diagnostics["stages"][0]["stdout"] == "pytest passed\n"
    assert diagnostics["stages"][1]["returncode"] == 7
    assert diagnostics["stages"][1]["stdout"] == "ruff stdout\n"
    assert diagnostics["stages"][1]["stderr"] == "ruff failed in full detail\n"
    assert not Path(diagnostics["temporary_workspace"]).exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert list(temp_root.iterdir()) == [sentinel]
