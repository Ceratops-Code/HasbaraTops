import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from hasbaratops.enums import CaseStatus, OutcomeClass, TurnDirection, TurnState
from hasbaratops.errors import HasbaraTopsError, StorageError, WriteSafetyError
from hasbaratops.storage import SQLiteStore
from hasbaratops.validation import validate_case, validate_turn
from tests.helpers import make_case, make_reply, make_turn


def test_database_initialization_requires_approval_and_reports_integrity(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "HasbaraTops.sqlite3")
    with pytest.raises(WriteSafetyError, match="approval"):
        store.initialize(approved=False)

    status = store.initialize(approved=True)
    assert status["integrity"] == "ok"
    assert status["schema_version"] == 1
    assert status["case_count"] == 0


def test_case_creation_is_atomic_allows_root_candidates_and_reads_back(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "HasbaraTops.sqlite3")
    store.initialize(approved=True)
    case = make_case()
    turn = make_turn()

    receipt = store.create_case(case, [turn], approved=True)
    assert receipt == {
        "created": True,
        "case_id": "Case-001",
        "turn_count": 1,
        "readback": "verified",
    }
    assert store.get_case(case.case_id) == case
    assert store.get_turns(case.case_id) == [turn]

    candidate = make_case(case_id="Case-002")
    store.create_case(candidate, [], approved=True)
    assert [item.case_id for item in store.find_cases("123", "456")] == [
        "Case-001",
        "Case-002",
    ]


def test_branch_split_allocates_case_copies_ancestors_and_verifies_backup(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "HasbaraTops.sqlite3")
    store.initialize(approved=True)
    case = make_case(status=CaseStatus.ACTIVE_EXCHANGE)
    root = make_turn()
    shared_reply = make_reply(
        "T002",
        "T001",
        participant_ref="USER",
        direction=TurnDirection.OUTGOING,
        state=TurnState.POSTED,
        exact_text="Shared reply.",
    )
    first_branch = make_reply("T003", "T002", exact_text="First branch.")
    second_branch = make_reply("T004", "T002", exact_text="Second branch.")
    first_answer = make_reply(
        "T005",
        "T003",
        participant_ref="USER",
        direction=TurnDirection.OUTGOING,
        state=TurnState.POSTED,
        exact_text="First branch answer.",
    )
    store.create_case(
        case,
        [root, shared_reply, first_branch, second_branch, first_answer],
        approved=True,
    )
    backup = tmp_path / "backups" / "before-split.sqlite3"

    with pytest.raises(WriteSafetyError, match="approval"):
        store.split_case_branch(
            "Case-001",
            "T004",
            new_case_title="Second branch Case",
            new_topic="Second branch topic",
            backup_destination=backup,
            approved=False,
        )

    receipt = store.split_case_branch(
        "Case-001",
        "T004",
        new_case_title="Second branch Case",
        new_topic="Second branch topic",
        backup_destination=backup,
        approved=True,
    )

    assert receipt["new_case_id"] == "Case-002"
    assert receipt["new_branch_root_turn_id"] == "T003"
    assert receipt["backup_verified"] is True
    assert SQLiteStore(backup).status()["case_count"] == 1
    assert [turn.turn_id for turn in store.get_turns("Case-001")] == [
        "T001",
        "T002",
        "T003",
        "T005",
    ]
    new_turns = store.get_turns("Case-002")
    assert [(turn.turn_id, turn.parent_turn_id, turn.exact_text) for turn in new_turns] == [
        ("T001", None, root.exact_text),
        ("T002", "T001", shared_reply.exact_text),
        ("T003", "T002", second_branch.exact_text),
    ]
    assert store.get_case("Case-002").case_title == "Second branch Case"
    assert [item.case_id for item in store.find_cases("123", "456")] == [
        "Case-001",
        "Case-002",
    ]


def test_branch_split_failure_rolls_back_and_retains_verified_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteStore(tmp_path / "HasbaraTops.sqlite3")
    store.initialize(approved=True)
    case = make_case(status=CaseStatus.ACTIVE_EXCHANGE)
    turns = [
        make_turn(),
        make_reply("T002", "T001", exact_text="Shared reply."),
        make_reply("T003", "T002", exact_text="First branch."),
        make_reply("T004", "T002", exact_text="Second branch."),
    ]
    store.create_case(case, turns, approved=True)
    backup = tmp_path / "backups" / "before-failed-split.sqlite3"

    def fail_insert(_connection: sqlite3.Connection, _record: object) -> None:
        raise sqlite3.IntegrityError("synthetic insert failure")

    monkeypatch.setattr(store, "_insert_turn", fail_insert)
    with pytest.raises(StorageError, match="rollback verified; backup retained"):
        store.split_case_branch(
            "Case-001",
            "T004",
            new_case_title="Second branch Case",
            new_topic="Second branch topic",
            backup_destination=backup,
            approved=True,
        )

    assert backup.is_file()
    assert SQLiteStore(backup).status()["case_count"] == 1
    assert store.case_ids() == ["Case-001"]
    assert store.get_turns("Case-001") == turns
    assert store.status()["integrity"] == "ok"


def test_schema_version_mismatch_blocks_reads(tmp_path: Path) -> None:
    path = tmp_path / "HasbaraTops.sqlite3"
    store = SQLiteStore(path)
    store.initialize(approved=True)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(StorageError, match="schema version mismatch"):
        store.status()


def test_initialization_refuses_an_unrelated_database(tmp_path: Path) -> None:
    path = tmp_path / "unrelated.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")

    with pytest.raises(StorageError, match="non-empty unversioned"):
        SQLiteStore(path).initialize(approved=True)


def test_followup_updates_status_and_verifies_committed_turn(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "HasbaraTops.sqlite3")
    store.initialize(approved=True)
    case = make_case()
    first = make_turn()
    store.create_case(case, [first], approved=True)
    followup = make_reply(
        "T002",
        "T001",
        observed_at="2026-07-17 11:00",
    )

    receipt = store.add_turn(
        followup,
        target_status=CaseStatus.ACTIVE_EXCHANGE,
        updated_at=followup.observed_at,
        reason="incoming public turn",
        replace_draft_id=None,
        approved=True,
    )

    assert receipt["readback"] == "verified"
    assert store.get_case(case.case_id).status is CaseStatus.ACTIVE_EXCHANGE
    assert store.get_turns(case.case_id)[-1] == followup


def test_posting_retires_named_draft_in_same_transaction(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "HasbaraTops.sqlite3")
    store.initialize(approved=True)
    case = make_case(status=CaseStatus.ACTIVE_EXCHANGE)
    first = make_turn()
    draft = make_reply(
        "T002",
        "T001",
        participant_ref="USER",
        direction=TurnDirection.OUTGOING,
        state=TurnState.DRAFT,
    )
    store.create_case(case, [first, draft], approved=True)
    posted = make_reply(
        "T003",
        "T001",
        participant_ref="USER",
        direction=TurnDirection.OUTGOING,
        state=TurnState.POSTED,
        exact_text="Exact published wording.",
        observed_at="2026-07-17 12:00",
    )

    store.add_turn(
        posted,
        target_status=CaseStatus.ACTIVE_EXCHANGE,
        updated_at=posted.observed_at,
        reason="posting confirmed",
        replace_draft_id="T002",
        approved=True,
    )

    turns = store.get_turns(case.case_id)
    assert turns[1].state is TurnState.REPLACED
    assert turns[2] == posted


def test_close_case_writes_only_validated_outcome_and_reads_back(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "HasbaraTops.sqlite3")
    store.initialize(approved=True)
    case = make_case(status=CaseStatus.ACTIVE_EXCHANGE)
    store.create_case(case, [make_turn()], approved=True)
    closed = replace(
        case,
        status=CaseStatus.CLOSED_SUBSTANTIVE,
        updated_at="2026-07-18 10:00",
        outcome_score=3,
        outcome_class=OutcomeClass.SUBSTANTIVE_ENGAGEMENT,
        outcome_notes="Observable exchange of reasons.",
        what_worked="Narrow question.",
        what_failed="Long setup.",
        next_test="Lead with the narrow question.",
        closed_at="2026-07-18 10:00",
    )

    receipt = store.close_case(closed, reason="explicit closeout", approved=True)

    assert receipt["readback"] == "verified"
    assert store.get_case(case.case_id) == closed


def test_open_case_returns_latest_turn_permalink_without_root_fallback(
    tmp_path: Path,
) -> None:
    invalid = make_case(post_url="https://www.facebook.com/example/posts/123")
    with pytest.raises(HasbaraTopsError, match="comment or reply"):
        validate_case(invalid)

    store = SQLiteStore(tmp_path / "HasbaraTops.sqlite3")
    store.initialize(approved=True)
    case = make_case(
        post_url=(
            "https://www.facebook.com/example/posts/123?comment_id=456"
            "&__cft__[0]=tracking#comment"
        )
    )
    root = make_turn(exact_url=case.post_url)
    reply_url = (
        "https://www.facebook.com/example/posts/123"
        "?comment_id=456&reply_comment_id=789"
    )
    reply = make_reply(
        "T002",
        "T001",
        exact_url=reply_url,
        reply_comment_id="789",
        observed_at="2026-07-17 11:00",
    )
    later_draft = make_reply(
        "T003",
        "T002",
        direction=TurnDirection.OUTGOING,
        state=TurnState.DRAFT,
        exact_url=(
            "https://www.facebook.com/example/posts/123"
            "?comment_id=456&reply_comment_id=999"
        ),
        reply_comment_id="999",
        observed_at="2026-07-17 12:00",
    )
    store.create_case(case, [root, reply, later_draft], approved=True)
    missing_case = make_case(case_id="Case-002")
    missing_turn = make_turn(
        case_id="Case-002",
        exact_text="Latest link was not supplied.",
    )
    store.create_case(missing_case, [missing_turn], approved=True)
    assert store.list_open_summaries() == [
        {
            "case_id": case.case_id,
            "status": "Posted",
            "last_turn_id": "T002",
            "last_comment_permalink": reply_url,
            "permalink_status": "supplied",
        },
        {
            "case_id": "Case-002",
            "status": "Posted",
            "last_turn_id": "T001",
            "last_comment_permalink": None,
            "permalink_status": "missing",
        },
    ]


def test_backup_is_consistent_and_never_overwrites(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "HasbaraTops.sqlite3")
    store.initialize(approved=True)
    store.create_case(make_case(), [], approved=True)
    destination = tmp_path / "backups" / "snapshot.sqlite3"

    store.backup(destination, approved=True)
    assert SQLiteStore(destination).status()["case_count"] == 1
    with pytest.raises(StorageError, match="already exists"):
        store.backup(destination, approved=True)


def test_turn_duplicate_identity_uses_reply_id_then_null_parent_fallback(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "HasbaraTops.sqlite3")
    store.initialize(approved=True)
    case = make_case()
    root = make_turn()
    store.create_case(case, [root], approved=True)

    fallback_duplicate = replace(root, turn_id="ignored", observed_at="later")
    receipt = store.add_turn(
        fallback_duplicate,
        target_status=CaseStatus.ACTIVE_EXCHANGE,
        updated_at="later",
        reason="duplicate supplied content",
        replace_draft_id=None,
        approved=True,
    )
    assert receipt == {
        "created": False,
        "duplicate": True,
        "duplicate_identity": "fallback",
        "case_id": "Case-001",
        "turn_id": "T001",
    }

    strong = make_reply(
        "ignored",
        "T001",
        reply_comment_id="789",
        exact_url=(
            "https://www.facebook.com/example/posts/123"
            "?comment_id=456&reply_comment_id=789"
        ),
        exact_text="First observed reply text.",
    )
    store.add_turn(
        strong,
        target_status=CaseStatus.ACTIVE_EXCHANGE,
        updated_at=strong.observed_at,
        reason="incoming public turn",
        replace_draft_id=None,
        approved=True,
    )
    changed_text = replace(strong, turn_id="ignored", exact_text="Edited observation.")
    duplicate = store.add_turn(
        changed_text,
        target_status=CaseStatus.ACTIVE_EXCHANGE,
        updated_at=changed_text.observed_at,
        reason="incoming public turn",
        replace_draft_id=None,
        approved=True,
    )
    assert duplicate["duplicate"] is True
    assert duplicate["duplicate_identity"] == "reply_comment_id"
    assert duplicate["turn_id"] == "T002"

    other_case = make_case(
        case_id="Case-002",
        post_url="https://www.facebook.com/example/posts/123?comment_id=999",
        root_comment_id="999",
    )
    store.create_case(other_case, [], approved=True)
    other_root = make_turn(
        case_id="Case-002",
        root_comment_id="999",
        exact_text=root.exact_text,
    )
    other_receipt = store.add_turn(
        other_root,
        target_status=CaseStatus.ACTIVE_EXCHANGE,
        updated_at=other_root.observed_at,
        reason="same fallback tuple in another Case",
        replace_draft_id=None,
        approved=True,
    )
    assert other_receipt["created"] is True
    cross_case = make_turn(
        case_id="Case-002",
        root_comment_id="999",
        reply_comment_id="789",
        exact_url=(
            "https://www.facebook.com/example/posts/123"
            "?comment_id=999&reply_comment_id=789"
        ),
        exact_text="Same Facebook reply in another explicit Case.",
    )
    with pytest.raises(StorageError, match="different Case"):
        store.add_turn(
            cross_case,
            target_status=CaseStatus.ACTIVE_EXCHANGE,
            updated_at=cross_case.observed_at,
            reason="incoming public turn",
            replace_draft_id=None,
            approved=True,
        )


def test_permalink_reply_id_cannot_be_omitted_or_conflict() -> None:
    exact_url = (
        "https://www.facebook.com/example/posts/123"
        "?comment_id=456&reply_comment_id=789"
    )
    with pytest.raises(HasbaraTopsError, match="must match"):
        validate_turn(make_turn(exact_url=exact_url))
    with pytest.raises(HasbaraTopsError, match="must match"):
        validate_turn(make_turn(exact_url=exact_url, reply_comment_id="999"))
    validate_turn(make_turn(exact_url=exact_url, reply_comment_id="789"))


def test_posting_unchanged_draft_promotes_and_enriches_same_turn(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "HasbaraTops.sqlite3")
    store.initialize(approved=True)
    case = make_case(status=CaseStatus.ACTIVE_EXCHANGE)
    root = make_turn()
    draft = make_reply(
        "T002",
        "T001",
        participant_ref="USER",
        direction=TurnDirection.OUTGOING,
        state=TurnState.DRAFT,
        exact_text="Unchanged exact wording.",
    )
    store.create_case(case, [root, draft], approved=True)
    posted = replace(
        draft,
        turn_id="ignored",
        state=TurnState.POSTED,
        reply_comment_id="900",
        exact_url=(
            "https://www.facebook.com/example/posts/123"
            "?comment_id=456&reply_comment_id=900"
        ),
        url_supplied_at="2026-07-17 12:00",
        observed_at="2026-07-17 12:00",
    )

    receipt = store.add_turn(
        posted,
        target_status=CaseStatus.ACTIVE_EXCHANGE,
        updated_at=posted.observed_at,
        reason="posting confirmed",
        replace_draft_id="T002",
        approved=True,
    )

    assert receipt["promoted"] is True
    assert receipt["turn_id"] == "T002"
    turns = store.get_turns(case.case_id)
    assert len(turns) == 2
    assert turns[1].state is TurnState.POSTED
    assert turns[1].reply_comment_id == "900"


def test_status_rejects_identity_index_tampering(tmp_path: Path) -> None:
    path = tmp_path / "HasbaraTops.sqlite3"
    store = SQLiteStore(path)
    store.initialize(approved=True)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX turns_fallback_identity_uq")
    with pytest.raises(StorageError, match="indexes"):
        store.status()
