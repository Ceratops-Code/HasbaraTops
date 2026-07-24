import json
from pathlib import Path

import pytest

from hasbaratops.cli import main
from hasbaratops.enums import CaseStatus, OutcomeClass, TurnDirection, TurnState
from hasbaratops.models import to_jsonable
from hasbaratops.storage import SQLiteStore
from tests.helpers import make_case, make_reply, make_turn


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(to_jsonable(value), ensure_ascii=False), encoding="utf-8")
    return path


def _run_ok(capsys: pytest.CaptureFixture[str], *args: str) -> object:
    assert main(list(args)) == 0
    return json.loads(capsys.readouterr().out)


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def test_transactional_case_cli_lifecycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "canonical.sqlite3"
    db = ("--database", str(database))
    initialized = _as_dict(_run_ok(capsys, *db, "db-init", "--approved"))
    assert initialized["integrity"] == "ok"
    assert _as_dict(_run_ok(capsys, *db, "check"))["ok"] is True

    case_payload = _as_dict(to_jsonable(make_case()))
    case_payload.pop("case_id")
    turn_payload = _as_dict(to_jsonable(make_turn()))
    intake_path = _write(
        tmp_path / "intake.json",
        {"case": case_payload, "turns": [turn_payload]},
    )
    intake = _as_dict(
        _run_ok(
            capsys,
            *db,
            "case-intake",
            str(intake_path),
            "--approved",
        )
    )
    assert intake == {
        "created": True,
        "case_id": "Case-001",
        "turn_count": 1,
        "readback": "verified",
    }

    second = _as_dict(
        _run_ok(
            capsys,
            *db,
            "case-intake",
            str(intake_path),
            "--approved",
        )
    )
    assert second["created"] is True
    assert second["case_id"] == "Case-002"

    open_cases = _run_ok(capsys, *db, "case-list-open")
    assert open_cases == [
        {
            "case_id": "Case-001",
            "status": "Posted",
            "last_turn_id": "T001",
            "last_comment_permalink": None,
            "permalink_status": "missing",
        },
        {
            "case_id": "Case-002",
            "status": "Posted",
            "last_turn_id": "T001",
            "last_comment_permalink": None,
            "permalink_status": "missing",
        },
    ]
    found = _as_dict(
        _run_ok(
            capsys,
            *db,
            "case-find",
            "--post-id",
            "123",
            "--root-comment-id",
            "456",
        )
    )
    assert found["candidate_count"] == 2
    candidate_rows = found["candidates"]
    assert isinstance(candidate_rows, list)
    assert [item["case_id"] for item in candidate_rows] == [
        "Case-001",
        "Case-002",
    ]
    definitive = _as_dict(
        _run_ok(capsys, *db, "case-find", "--case-id", "Case-001")
    )
    assert definitive["case_id"] == "Case-001"

    followup = make_reply(
        "ignored",
        "T001",
        observed_at="2026-07-17 11:00",
        exact_text="Synthetic follow-up.",
        exact_url=(
            "https://www.facebook.com/example/posts/123"
            "?comment_id=456&reply_comment_id=789"
        ),
    )
    followup_path = _write(tmp_path / "followup.json", followup)
    followup_receipt = _as_dict(
        _run_ok(
            capsys,
            *db,
            "case-followup",
            "--case-id",
            "Case-001",
            str(followup_path),
            "--approved",
        )
    )
    assert followup_receipt["turn_id"] == "T002"
    assert followup_receipt["status"] == "Active Exchange"
    shown = _as_dict(
        _run_ok(capsys, *db, "case-show", "--case-id", "Case-001")
    )
    assert shown["turns"][1]["reply_comment_id"] == "789"  # type: ignore[index]

    posted = make_reply(
        "ignored",
        "T002",
        participant_ref="USER",
        direction=TurnDirection.OUTGOING,
        state=TurnState.POSTED,
        exact_text="Exact published reply.",
        observed_at="2026-07-17 12:00",
    )
    posting_path = _write(tmp_path / "posting.json", posted)
    posting_receipt = _as_dict(
        _run_ok(
            capsys,
            *db,
            "case-record-posting",
            "--case-id",
            "Case-001",
            str(posting_path),
            "--approved",
        )
    )
    assert posting_receipt["turn_id"] == "T003"
    assert posting_receipt["readback"] == "verified"

    close_path = _write(
        tmp_path / "close.json",
        {
            "status": CaseStatus.CLOSED_SUBSTANTIVE,
            "updated_at": "2026-07-18 10:00",
            "outcome_score": 3,
            "outcome_class": OutcomeClass.SUBSTANTIVE_ENGAGEMENT,
            "outcome_notes": "Observable exchange of reasons.",
            "what_worked": "Narrow question.",
            "what_failed": "Long setup.",
            "next_test": "Lead with the narrow question.",
            "closed_at": "2026-07-18 10:00",
            "reason": "explicit closeout",
        },
    )
    close_receipt = _as_dict(
        _run_ok(
            capsys,
            *db,
            "case-close",
            "--case-id",
            "Case-001",
            str(close_path),
            "--approved",
        )
    )
    assert close_receipt["status"] == "Closed - Substantive"
    assert len(_run_ok(capsys, *db, "case-list-open")) == 1  # type: ignore[arg-type]
    dataset = _as_dict(_run_ok(capsys, *db, "strategy-dataset"))
    assert len(dataset["cases"]) == 1  # type: ignore[arg-type]
    assert len(dataset["turns"]["Case-001"]) == 3  # type: ignore[index]


def test_case_split_branch_cli_returns_verified_mapping(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "canonical.sqlite3"
    store = SQLiteStore(database)
    store.initialize(approved=True)
    case = make_case(status=CaseStatus.ACTIVE_EXCHANGE)
    turns = [
        make_turn(),
        make_reply(
            "T002",
            "T001",
            participant_ref="USER",
            direction=TurnDirection.OUTGOING,
            state=TurnState.POSTED,
            exact_text="Shared reply.",
        ),
        make_reply("T003", "T002", exact_text="First branch."),
        make_reply("T004", "T002", exact_text="Second branch."),
    ]
    store.create_case(case, turns, approved=True)
    backup = tmp_path / "before-split.sqlite3"

    receipt = _as_dict(
        _run_ok(
            capsys,
            "--database",
            str(database),
            "case-split-branch",
            "--case-id",
            "Case-001",
            "--branch-root-turn-id",
            "T004",
            "--new-case-title",
            "Second branch Case",
            "--new-topic",
            "Second branch topic",
            "--backup-destination",
            str(backup),
            "--approved",
        )
    )

    assert receipt["source_case_id"] == "Case-001"
    assert receipt["new_case_id"] == "Case-002"
    assert receipt["new_branch_root_turn_id"] == "T003"
    assert receipt["committed_read_back"] == "verified"
    assert backup.is_file()


def test_pure_validation_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids_path = _write(tmp_path / "case-ids.json", ["Case-001"])
    assert _as_dict(
        _run_ok(
            capsys,
            "next-case-id",
            "--existing",
            str(ids_path),
        )
    )["case_id"] == "Case-002"
    turns_path = _write(
        tmp_path / "turn-ids.json",
        [{"case_id": "Case-001", "turn_id": "T001"}],
    )
    assert _as_dict(
        _run_ok(
            capsys,
            "next-turn-id",
            "--case-id",
            "Case-001",
            "--existing",
            str(turns_path),
        )
    )["turn_id"] == "T002"

    case_path = _write(tmp_path / "case.json", make_case())
    assert _as_dict(_run_ok(capsys, "validate-case", str(case_path)))["valid"] is True
    turn = make_turn()
    turn_path = _write(tmp_path / "turn.json", turn)
    assert _as_dict(_run_ok(capsys, "validate-turn", str(turn_path)))["valid"] is True
    transition_path = _write(
        tmp_path / "transition.json",
        {
            "from_status": CaseStatus.POSTED,
            "to_status": CaseStatus.ACTIVE_EXCHANGE,
            "reason": "new turn",
        },
    )
    assert _as_dict(
        _run_ok(capsys, "validate-transition", str(transition_path))
    )["valid"] is True
    graph_path = _write(
        tmp_path / "graph.json",
        [
            turn,
            make_reply(
                "T002",
                "T001",
                participant_ref="USER",
                direction=TurnDirection.OUTGOING,
                state=TurnState.POSTED,
            ),
        ],
    )
    assert _as_dict(
        _run_ok(capsys, "validate-parent-graph", str(graph_path))
    )["turn_count"] == 2

    expected_path = _write(tmp_path / "expected.json", {"turn_id": "T001"})
    actual_path = _write(tmp_path / "actual.json", {"turn_id": "T001"})
    assert _as_dict(
        _run_ok(
            capsys,
            "verify-readback",
            "--expected",
            str(expected_path),
            "--actual",
            str(actual_path),
        )
    )["succeeded"] is True

def test_write_command_rejects_missing_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "canonical.sqlite3"
    assert main(["--database", str(database), "db-init"]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["category"] == "write_safety_error"
    assert not database.exists()
