"""Transactional SQLite storage for canonical HasbaraTops case state.

All mutations require an explicit approval flag, run inside an immediate
transaction, and are read back after commit. The caller supplies the database
path; this module never discovers credentials, contacts a network service, or
writes outside that path and explicitly requested backup destinations.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path

from .enums import (
    CaseStatus,
    OutcomeClass,
    ParentConfidence,
    TurnDirection,
    TurnKind,
    TurnState,
)
from .errors import StorageError, WriteSafetyError
from .identifiers import next_case_id, next_turn_id, now_jerusalem
from .lifecycle import CLOSED_STATUSES, validate_transition
from .models import CaseRecord, LifecycleTransition, TurnRecord, to_jsonable
from .parent_graph import validate_parent_graph
from .readback import verify_readback
from .schema import CASE_FIELDS, SCHEMA_VERSION, TURN_FIELDS, schema_signature
from .validation import validate_case, validate_turn


def _sql_enum(values: Iterable[str]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


_STATUS_SQL = _sql_enum(item.value for item in CaseStatus)
_OUTCOME_SQL = _sql_enum(item.value for item in OutcomeClass)
_CONFIDENCE_SQL = _sql_enum(item.value for item in ParentConfidence)
_DIRECTION_SQL = _sql_enum(item.value for item in TurnDirection)
_KIND_SQL = _sql_enum(item.value for item in TurnKind)
_STATE_SQL = _sql_enum(item.value for item in TurnState)
_SCHEMA_SIGNATURE = schema_signature().value

_CASES_SQL = f"""
CREATE TABLE {{if_not_exists}}{{table}} (
    case_id TEXT PRIMARY KEY CHECK (
        case_id = printf('Case-%03d', CAST(substr(case_id, 6) AS INTEGER))
        AND CAST(substr(case_id, 6) AS INTEGER) > 0
    ),
    case_title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ({_STATUS_SQL})),
    topic TEXT NOT NULL,
    post_text TEXT NOT NULL,
    post_url TEXT NOT NULL,
    post_id TEXT NOT NULL,
    root_comment_id TEXT NOT NULL,
    source_links TEXT NOT NULL DEFAULT '[]',
    privacy_checked INTEGER NOT NULL CHECK (privacy_checked IN (0, 1)),
    outcome_score INTEGER CHECK (outcome_score BETWEEN 0 AND 5),
    outcome_class TEXT CHECK (outcome_class IS NULL OR outcome_class IN ({_OUTCOME_SQL})),
    outcome_notes TEXT NOT NULL DEFAULT '',
    user_rating INTEGER CHECK (user_rating BETWEEN 1 AND 5),
    what_worked TEXT NOT NULL DEFAULT '',
    what_failed TEXT NOT NULL DEFAULT '',
    next_test TEXT NOT NULL DEFAULT '',
    closed_at TEXT NOT NULL DEFAULT ''
)
"""

_TURNS_SQL = f"""
CREATE TABLE {{if_not_exists}}{{table}} (
    case_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    parent_turn_id TEXT,
    parent_confidence TEXT CHECK (
        parent_confidence IS NULL OR parent_confidence IN ({_CONFIDENCE_SQL})
    ),
    participant_ref TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ({_DIRECTION_SQL})),
    kind TEXT NOT NULL CHECK (kind IN ({_KIND_SQL})),
    state TEXT NOT NULL CHECK (state IN ({_STATE_SQL})),
    exact_text TEXT NOT NULL,
    post_id TEXT NOT NULL,
    root_comment_id TEXT NOT NULL,
    reply_comment_id TEXT,
    exact_url TEXT,
    url_supplied_at TEXT,
    observed_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (case_id, turn_id),
    FOREIGN KEY (case_id) REFERENCES {{cases_table}}(case_id) ON DELETE RESTRICT,
    FOREIGN KEY (case_id, parent_turn_id) REFERENCES {{table}}(case_id, turn_id)
        DEFERRABLE INITIALLY DEFERRED
)
"""

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS storage_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

{_CASES_SQL.format(if_not_exists="IF NOT EXISTS ", table="cases")};
{_TURNS_SQL.format(if_not_exists="IF NOT EXISTS ", table="turns", cases_table="cases")};

CREATE INDEX IF NOT EXISTS turns_case_observed_idx
    ON turns(case_id, observed_at, turn_id);
CREATE INDEX IF NOT EXISTS cases_root_candidates_idx
    ON cases(post_id, root_comment_id, created_at, case_id);
CREATE UNIQUE INDEX IF NOT EXISTS turns_reply_comment_id_uq
    ON turns(reply_comment_id) WHERE reply_comment_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS turns_fallback_identity_uq
    ON turns(case_id, COALESCE(parent_turn_id, ''), direction, exact_text)
    WHERE reply_comment_id IS NULL;
INSERT INTO storage_metadata(key, value)
    VALUES ('schema_signature', '{_SCHEMA_SIGNATURE}')
    ON CONFLICT(key) DO NOTHING;
PRAGMA user_version = {SCHEMA_VERSION};
"""

_EXPECTED_INDEX_SQL = {
    "turns_case_observed_idx": """CREATE INDEX turns_case_observed_idx
        ON turns(case_id, observed_at, turn_id)""",
    "cases_root_candidates_idx": """CREATE INDEX cases_root_candidates_idx
        ON cases(post_id, root_comment_id, created_at, case_id)""",
    "turns_reply_comment_id_uq": """CREATE UNIQUE INDEX turns_reply_comment_id_uq
        ON turns(reply_comment_id) WHERE reply_comment_id IS NOT NULL""",
    "turns_fallback_identity_uq": """CREATE UNIQUE INDEX turns_fallback_identity_uq
        ON turns(case_id, COALESCE(parent_turn_id, ''), direction, exact_text)
        WHERE reply_comment_id IS NULL""",
}


def _normalized_sql(value: str) -> str:
    return " ".join(value.lower().replace("if not exists ", "").split())


def _require_approval(approved: bool) -> None:
    if not approved:
        raise WriteSafetyError("canonical SQLite writes require explicit approval")


def _rows(
    connection: sqlite3.Connection, table: str, fields: tuple[str, ...], order_by: str
) -> list[dict[str, object]]:
    selected = ", ".join(fields)
    return [
        {field: row[field] for field in fields}
        for row in connection.execute(
            f"SELECT {selected} FROM {table} ORDER BY {order_by}"  # noqa: S608
        )
    ]


def _snapshot(connection: sqlite3.Connection) -> dict[str, list[dict[str, object]]]:
    return {
        "cases": _rows(connection, "cases", CASE_FIELDS, "created_at, case_id"),
        "turns": _rows(connection, "turns", TURN_FIELDS, "case_id, turn_id"),
    }


def _verify_database_shape(
    connection: sqlite3.Connection, *, expected_signature: str
) -> dict[str, object]:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_VERSION:
        raise StorageError(
            f"database schema version mismatch: {version}; expected {SCHEMA_VERSION}"
        )
    signature_row = connection.execute(
        "SELECT value FROM storage_metadata WHERE key = 'schema_signature'"
    ).fetchone()
    actual_signature = str(signature_row["value"]) if signature_row else ""
    if actual_signature != expected_signature:
        raise StorageError("database schema signature mismatch")
    case_columns = tuple(
        str(row["name"]) for row in connection.execute("PRAGMA table_info(cases)")
    )
    turn_columns = tuple(
        str(row["name"]) for row in connection.execute("PRAGMA table_info(turns)")
    )
    if case_columns != CASE_FIELDS or turn_columns != TURN_FIELDS:
        raise StorageError("database table columns do not match the canonical schema")
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or foreign_keys:
        raise StorageError("database integrity check failed")
    return {
        "schema_version": version,
        "schema_signature": actual_signature,
        "integrity": integrity,
        "case_count": int(connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0]),
        "turn_count": int(connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0]),
    }


def _case_values(record: CaseRecord) -> dict[str, object]:
    return {
        "case_id": record.case_id,
        "case_title": record.case_title,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "status": record.status.value,
        "topic": record.topic,
        "post_text": record.post_text,
        "post_url": record.post_url,
        "post_id": record.post_id,
        "root_comment_id": record.root_comment_id,
        "source_links": json.dumps(list(record.source_links), ensure_ascii=False),
        "privacy_checked": int(record.privacy_checked),
        "outcome_score": record.outcome_score,
        "outcome_class": record.outcome_class.value if record.outcome_class else None,
        "outcome_notes": record.outcome_notes,
        "user_rating": record.user_rating,
        "what_worked": record.what_worked,
        "what_failed": record.what_failed,
        "next_test": record.next_test,
        "closed_at": record.closed_at,
    }


def _turn_values(record: TurnRecord) -> dict[str, object]:
    return {
        "case_id": record.case_id,
        "turn_id": record.turn_id,
        "parent_turn_id": record.parent_turn_id,
        "parent_confidence": (
            record.parent_confidence.value if record.parent_confidence else None
        ),
        "participant_ref": record.participant_ref,
        "direction": record.direction.value,
        "kind": record.kind.value,
        "state": record.state.value,
        "exact_text": record.exact_text,
        "post_id": record.post_id,
        "root_comment_id": record.root_comment_id,
        "reply_comment_id": record.reply_comment_id,
        "exact_url": record.exact_url,
        "url_supplied_at": record.url_supplied_at,
        "observed_at": record.observed_at,
        "notes": record.notes,
    }


def _case_from_row(row: sqlite3.Row) -> CaseRecord:
    raw_links = json.loads(str(row["source_links"]))
    if not isinstance(raw_links, list):
        raise StorageError(f"invalid source_links JSON for {row['case_id']}")
    return CaseRecord(
        case_id=str(row["case_id"]),
        case_title=str(row["case_title"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        status=CaseStatus(str(row["status"])),
        topic=str(row["topic"]),
        post_text=str(row["post_text"]),
        post_url=str(row["post_url"]),
        post_id=str(row["post_id"]),
        root_comment_id=str(row["root_comment_id"]),
        source_links=tuple(str(item) for item in raw_links),
        privacy_checked=bool(row["privacy_checked"]),
        outcome_score=(int(row["outcome_score"]) if row["outcome_score"] is not None else None),
        outcome_class=(
            OutcomeClass(str(row["outcome_class"]))
            if row["outcome_class"] is not None
            else None
        ),
        outcome_notes=str(row["outcome_notes"]),
        user_rating=(int(row["user_rating"]) if row["user_rating"] is not None else None),
        what_worked=str(row["what_worked"]),
        what_failed=str(row["what_failed"]),
        next_test=str(row["next_test"]),
        closed_at=str(row["closed_at"]),
    )


def _turn_from_row(row: sqlite3.Row) -> TurnRecord:
    return TurnRecord(
        case_id=str(row["case_id"]),
        turn_id=str(row["turn_id"]),
        parent_turn_id=(str(row["parent_turn_id"]) if row["parent_turn_id"] else None),
        parent_confidence=(
            ParentConfidence(str(row["parent_confidence"]))
            if row["parent_confidence"] is not None
            else None
        ),
        participant_ref=str(row["participant_ref"]),
        direction=TurnDirection(str(row["direction"])),
        kind=TurnKind(str(row["kind"])),
        state=TurnState(str(row["state"])),
        exact_text=str(row["exact_text"]),
        post_id=str(row["post_id"]),
        root_comment_id=str(row["root_comment_id"]),
        reply_comment_id=(
            str(row["reply_comment_id"]) if row["reply_comment_id"] is not None else None
        ),
        exact_url=str(row["exact_url"]) if row["exact_url"] is not None else None,
        url_supplied_at=(
            str(row["url_supplied_at"]) if row["url_supplied_at"] is not None else None
        ),
        observed_at=str(row["observed_at"]),
        notes=str(row["notes"]),
    )


def _find_turn_duplicate(
    connection: sqlite3.Connection, turn: TurnRecord
) -> TurnRecord | None:
    if turn.reply_comment_id is not None:
        row = connection.execute(
            "SELECT * FROM turns WHERE reply_comment_id = ?",
            (turn.reply_comment_id,),
        ).fetchone()
    else:
        row = connection.execute(
            """SELECT * FROM turns
            WHERE case_id = ? AND parent_turn_id IS ? AND direction = ?
              AND exact_text = ? AND reply_comment_id IS NULL""",
            (
                turn.case_id,
                turn.parent_turn_id,
                turn.direction.value,
                turn.exact_text,
            ),
        ).fetchone()
    return _turn_from_row(row) if row is not None else None


def _duplicate_turn_receipt(
    duplicate: TurnRecord, requested: TurnRecord
) -> Mapping[str, object]:
    if duplicate.case_id != requested.case_id:
        raise StorageError(
            "reply_comment_id belongs to a different Case: "
            f"{duplicate.case_id}/{duplicate.turn_id}"
        )
    return {
        "created": False,
        "duplicate": True,
        "duplicate_identity": (
            "reply_comment_id" if requested.reply_comment_id is not None else "fallback"
        ),
        "case_id": duplicate.case_id,
        "turn_id": duplicate.turn_id,
    }


def _branch_partition(
    turns: list[TurnRecord], branch_root_turn_id: str
) -> tuple[list[TurnRecord], list[TurnRecord], list[TurnRecord]]:
    """Return copied ancestors, moved branch Turns, and source remainder.

    Shared ancestors must not carry globally unique reply identifiers because
    the split intentionally copies them into the new Case. The branch root must
    be a non-root Turn, and another branch must remain in the source Case.
    """
    validate_parent_graph(turns)
    by_id = {turn.turn_id: turn for turn in turns}
    branch_root = by_id.get(branch_root_turn_id)
    if branch_root is None:
        raise StorageError(f"branch root Turn not found: {branch_root_turn_id}")
    if branch_root.parent_turn_id is None:
        raise StorageError("branch split requires a non-root Turn")

    descendant_ids = {branch_root_turn_id}
    changed = True
    while changed:
        changed = False
        for turn in turns:
            if turn.parent_turn_id in descendant_ids and turn.turn_id not in descendant_ids:
                descendant_ids.add(turn.turn_id)
                changed = True

    ancestor_ids: list[str] = []
    parent_id: str | None = branch_root.parent_turn_id
    while parent_id is not None:
        ancestor = by_id[parent_id]
        ancestor_ids.append(parent_id)
        parent_id = ancestor.parent_turn_id
    ancestor_ids.reverse()
    ancestor_id_set = set(ancestor_ids)
    other_branch_ids = set(by_id) - descendant_ids - ancestor_id_set
    if not other_branch_ids:
        raise StorageError("branch split would leave no separate branch in the source Case")

    ancestors = [turn for turn in turns if turn.turn_id in ancestor_id_set]
    if any(turn.reply_comment_id is not None for turn in ancestors):
        raise StorageError(
            "shared branch ancestors with reply_comment_id cannot be copied across Cases"
        )
    moved = [turn for turn in turns if turn.turn_id in descendant_ids]
    remaining = [turn for turn in turns if turn.turn_id not in descendant_ids]
    validate_parent_graph(remaining)
    return ancestors, moved, remaining


class SQLiteStore:
    """Narrow canonical storage API; no generic SQL execution is exposed."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def _connect(
        self, *, require_exists: bool = True, writable: bool = False
    ) -> sqlite3.Connection:
        if require_exists and not self.path.is_file():
            raise StorageError(f"database does not exist: {self.path}")
        target = self.path if writable else f"{self.path.as_uri()}?mode=ro"
        connection = sqlite3.connect(
            target,
            timeout=5.0,
            isolation_level=None,
            uri=not writable,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self, *, approved: bool) -> Mapping[str, object]:
        """Create or verify the schema without replacing an existing database."""
        _require_approval(approved)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect(require_exists=False, writable=True) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, SCHEMA_VERSION}:
                raise StorageError(
                    f"unsupported database schema version: {version}; expected {SCHEMA_VERSION}"
                )
            if version == 0:
                tables = connection.execute(
                    """SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"""
                ).fetchall()
                if tables:
                    raise StorageError("refusing to initialize a non-empty unversioned database")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(_SCHEMA_SQL)
        return self.status()

    def status(self) -> Mapping[str, object]:
        with self._connect() as connection:
            state = _verify_database_shape(
                connection, expected_signature=_SCHEMA_SIGNATURE
            )
            named_indexes = {
                str(row["name"]): _normalized_sql(str(row["sql"]))
                for row in connection.execute(
                    """SELECT name, sql FROM sqlite_master
                    WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex%'"""
                )
            }
            expected_indexes = {
                name: _normalized_sql(sql) for name, sql in _EXPECTED_INDEX_SQL.items()
            }
            if named_indexes != expected_indexes:
                raise StorageError("database indexes do not match the canonical schema")
            cases_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cases'"
            ).fetchone()
            cases_sql = _normalized_sql(str(cases_sql_row["sql"]))
            if "case_id = printf('case-%03d'" not in cases_sql:
                raise StorageError("cases table lacks the canonical Case ID constraint")
            unexpected_unique_constraints = [
                row
                for row in connection.execute("PRAGMA index_list(cases)")
                if str(row["origin"]) == "u"
            ]
            if unexpected_unique_constraints:
                raise StorageError("cases table has an unexpected UNIQUE constraint")
            foreign_keys = sorted(
                (
                    str(row["table"]),
                    str(row["from"]),
                    str(row["to"]),
                    str(row["on_delete"]),
                )
                for row in connection.execute("PRAGMA foreign_key_list(turns)")
            )
            expected_foreign_keys = sorted(
                [
                    ("cases", "case_id", "case_id", "RESTRICT"),
                    ("turns", "case_id", "case_id", "NO ACTION"),
                    ("turns", "parent_turn_id", "turn_id", "NO ACTION"),
                ]
            )
            if foreign_keys != expected_foreign_keys:
                raise StorageError("database foreign keys do not match the canonical schema")
        return {
            "ok": True,
            **state,
        }

    def backup(self, destination: Path, *, approved: bool) -> Mapping[str, object]:
        """Create a verified backup, never overwrite, and remove partial output on failure."""
        _require_approval(approved)
        target = destination.resolve()
        if target.exists():
            raise StorageError(f"backup destination already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as source, sqlite3.connect(target) as backup:
                source.backup(backup)
            status = SQLiteStore(target).status()
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {"ok": True, "backup": str(target), "database": status}

    def case_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT case_id FROM cases "
                "ORDER BY CAST(substr(case_id, 6) AS INTEGER)"
            ).fetchall()
        return [str(row["case_id"]) for row in rows]

    def turn_ids(self, case_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT turn_id FROM turns WHERE case_id = ? ORDER BY turn_id", (case_id,)
            ).fetchall()
        return [str(row["turn_id"]) for row in rows]

    def _get_case(self, connection: sqlite3.Connection, case_id: str) -> CaseRecord:
        row = connection.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        if row is None:
            raise StorageError(f"case not found: {case_id}")
        return _case_from_row(row)

    def _get_turns(self, connection: sqlite3.Connection, case_id: str) -> list[TurnRecord]:
        rows = connection.execute(
            "SELECT * FROM turns WHERE case_id = ? ORDER BY turn_id", (case_id,)
        ).fetchall()
        return [_turn_from_row(row) for row in rows]

    def get_case(self, case_id: str) -> CaseRecord:
        with self._connect() as connection:
            return self._get_case(connection, case_id)

    def get_turns(self, case_id: str) -> list[TurnRecord]:
        with self._connect() as connection:
            self._get_case(connection, case_id)
            return self._get_turns(connection, case_id)

    def find_cases(self, post_id: str, root_comment_id: str) -> list[CaseRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM cases WHERE post_id = ? AND root_comment_id = ?
                ORDER BY created_at, case_id""",
                (post_id, root_comment_id),
            ).fetchall()
        return [_case_from_row(row) for row in rows]

    def find_turn_duplicate(self, turn: TurnRecord) -> TurnRecord | None:
        """Resolve a Turn by its strongest available immutable identity."""
        with self._connect() as connection:
            return _find_turn_duplicate(connection, turn)

    def list_open_summaries(self) -> list[dict[str, object]]:
        closed_statuses = tuple(sorted(status.value for status in CLOSED_STATUSES))
        closed_placeholders = ", ".join("?" for _ in closed_statuses)
        with self._connect() as connection:
            rows = connection.execute(
                f"""WITH ranked_public_turns AS (
                    SELECT case_id, turn_id, exact_url,
                        ROW_NUMBER() OVER (
                            PARTITION BY case_id
                            ORDER BY observed_at DESC,
                                CAST(substr(turn_id, 2) AS INTEGER) DESC
                        ) AS turn_rank
                    FROM turns
                    WHERE state NOT IN (?, ?)
                )
                SELECT cases.case_id, cases.status,
                    latest.turn_id AS last_turn_id,
                    latest.exact_url AS last_comment_permalink
                FROM cases
                LEFT JOIN ranked_public_turns AS latest
                    ON latest.case_id = cases.case_id
                    AND latest.turn_rank = 1
                WHERE cases.status NOT IN ({closed_placeholders})
                ORDER BY CAST(substr(cases.case_id, 6) AS INTEGER)""",
                (
                    TurnState.DRAFT.value,
                    TurnState.REPLACED.value,
                    *closed_statuses,
                ),
            ).fetchall()
        return [
            {
                "case_id": str(row["case_id"]),
                "status": str(row["status"]),
                "last_turn_id": row["last_turn_id"],
                "last_comment_permalink": row["last_comment_permalink"],
                "permalink_status": (
                    "supplied" if row["last_comment_permalink"] else "missing"
                ),
            }
            for row in rows
        ]

    def strategy_dataset(self) -> Mapping[str, object]:
        with self._connect() as connection:
            cases = [
                _case_from_row(row)
                for row in connection.execute(
                    "SELECT * FROM cases "
                    "ORDER BY CAST(substr(case_id, 6) AS INTEGER)"
                )
                if CaseStatus(str(row["status"])) in CLOSED_STATUSES
            ]
            turns = {
                case.case_id: self._get_turns(connection, case.case_id) for case in cases
            }
        return {"cases": cases, "turns": turns}

    @staticmethod
    def _insert_case(connection: sqlite3.Connection, record: CaseRecord) -> None:
        connection.execute(
            """INSERT INTO cases VALUES (
                :case_id, :case_title, :created_at, :updated_at, :status, :topic,
                :post_text, :post_url, :post_id, :root_comment_id, :source_links,
                :privacy_checked, :outcome_score, :outcome_class, :outcome_notes,
                :user_rating, :what_worked, :what_failed, :next_test, :closed_at
            )""",
            _case_values(record),
        )

    @staticmethod
    def _insert_turn(connection: sqlite3.Connection, record: TurnRecord) -> None:
        connection.execute(
            """INSERT INTO turns VALUES (
                :case_id, :turn_id, :parent_turn_id, :parent_confidence,
                :participant_ref, :direction, :kind, :state, :exact_text, :post_id,
                :root_comment_id, :reply_comment_id, :exact_url, :url_supplied_at,
                :observed_at, :notes
            )""",
            _turn_values(record),
        )

    @staticmethod
    def _validate_bundle(cases: list[CaseRecord], turns: list[TurnRecord]) -> None:
        by_case = {case.case_id: case for case in cases}
        if len(by_case) != len(cases):
            raise StorageError("duplicate Case IDs in write payload")
        grouped: dict[str, list[TurnRecord]] = defaultdict(list)
        for case in cases:
            validate_case(case)
        for turn in turns:
            validate_turn(turn)
            parent_case = by_case.get(turn.case_id)
            if parent_case is None:
                raise StorageError(f"turn references missing Case: {turn.case_id}")
            if (turn.post_id, turn.root_comment_id) != (
                parent_case.post_id,
                parent_case.root_comment_id,
            ):
                raise StorageError(f"turn identity conflicts with Case: {turn.turn_id}")
            grouped[turn.case_id].append(turn)
        for case_turns in grouped.values():
            validate_parent_graph(case_turns)

    def create_case(
        self,
        case: CaseRecord,
        turns: list[TurnRecord],
        *,
        approved: bool,
        allocate_ids: bool = False,
    ) -> Mapping[str, object]:
        """Atomically create one Case and its initial public-turn graph."""
        _require_approval(approved)
        try:
            with self._connect(writable=True) as connection:
                connection.execute("BEGIN IMMEDIATE")
                stored_case = case
                stored_turns = turns
                existing_case_ids = [
                    str(row["case_id"])
                    for row in connection.execute("SELECT case_id FROM cases")
                ]
                if allocate_ids:
                    stored_case = replace(
                        case, case_id=next_case_id(existing_case_ids)
                    )
                    stored_turns = []
                    for turn in turns:
                        stored_turns.append(
                            replace(
                                turn,
                                case_id=stored_case.case_id,
                                turn_id=next_turn_id(
                                    item.turn_id for item in stored_turns
                                ),
                            )
                        )
                elif case.case_id != next_case_id(existing_case_ids):
                    raise StorageError(
                        "case creation requires the next global sequential Case ID"
                    )
                self._validate_bundle([stored_case], stored_turns)
                self._insert_case(connection, stored_case)
                for turn in stored_turns:
                    self._insert_turn(connection, turn)
                connection.commit()
        except sqlite3.Error as error:
            raise StorageError(f"case creation failed: {error}") from error
        actual_case = self.get_case(stored_case.case_id)
        actual_turns = self.get_turns(stored_case.case_id)
        if actual_case != stored_case or actual_turns != sorted(
            stored_turns, key=lambda item: item.turn_id
        ):
            raise StorageError("case creation read-back mismatch")
        return {
            "created": True,
            "case_id": stored_case.case_id,
            "turn_count": len(stored_turns),
            "readback": "verified",
        }

    def split_case_branch(
        self,
        case_id: str,
        branch_root_turn_id: str,
        *,
        new_case_title: str,
        new_topic: str,
        backup_destination: Path,
        approved: bool,
    ) -> Mapping[str, object]:
        """Move one branch to a new Case while copying its shared ancestor path.

        The source is backed up and read back before an immediate transaction.
        The new Case receives the next global Case ID and fresh case-local Turn
        IDs; exact public text and URLs are preserved byte-for-byte.
        """
        _require_approval(approved)
        if not new_case_title.strip() or not new_topic.strip():
            raise StorageError("new Case title and topic are required")
        target = backup_destination.resolve()
        if target == self.path:
            raise StorageError("backup destination must differ from the canonical database")
        if target.exists():
            raise StorageError(f"backup destination already exists: {target}")

        with self._connect() as preflight:
            source_case = self._get_case(preflight, case_id)
            _branch_partition(self._get_turns(preflight, case_id), branch_root_turn_id)
            if source_case.status in CLOSED_STATUSES:
                raise StorageError("cannot split a closed Case")
        target.parent.mkdir(parents=True, exist_ok=True)

        source_snapshot: dict[str, list[dict[str, object]]] | None = None
        backup_verified = False
        new_case: CaseRecord | None = None
        updated_source: CaseRecord | None = None
        new_turns: list[TurnRecord] = []
        remaining_turns: list[TurnRecord] = []
        moved_turns: list[TurnRecord] = []
        ancestors: list[TurnRecord] = []
        new_branch_root_turn_id = ""
        try:
            with self._connect(writable=True) as connection:
                source_snapshot = _snapshot(connection)
                source_case = self._get_case(connection, case_id)
                source_turns = self._get_turns(connection, case_id)
                if source_case.status in CLOSED_STATUSES:
                    raise StorageError("cannot split a closed Case")
                ancestors, moved_turns, remaining_turns = _branch_partition(
                    source_turns, branch_root_turn_id
                )

                with sqlite3.connect(target) as backup:
                    connection.backup(backup)
                with sqlite3.connect(target) as backup_read:
                    backup_read.row_factory = sqlite3.Row
                    _verify_database_shape(
                        backup_read, expected_signature=_SCHEMA_SIGNATURE
                    )
                    if _snapshot(backup_read) != source_snapshot:
                        raise StorageError("branch split backup read-back mismatch")
                    backup_verified = True

                connection.execute("BEGIN IMMEDIATE")
                if _snapshot(connection) != source_snapshot:
                    raise StorageError("database changed while branch split was starting")
                existing_case_ids = [
                    str(row["case_id"])
                    for row in connection.execute("SELECT case_id FROM cases")
                ]
                new_case_id = next_case_id(existing_case_ids)
                split_at = now_jerusalem().strftime("%Y-%m-%d %H:%M")
                updated_source = replace(source_case, updated_at=split_at)
                new_case = replace(
                    source_case,
                    case_id=new_case_id,
                    case_title=new_case_title.strip(),
                    topic=new_topic.strip(),
                    created_at=split_at,
                    updated_at=split_at,
                )

                selected_turns = [*ancestors, *moved_turns]
                selected_ids = {turn.turn_id for turn in selected_turns}
                selected_turns = [
                    turn for turn in source_turns if turn.turn_id in selected_ids
                ]
                turn_id_map = {
                    turn.turn_id: f"T{index:03d}"
                    for index, turn in enumerate(selected_turns, start=1)
                }
                new_turns = [
                    replace(
                        turn,
                        case_id=new_case_id,
                        turn_id=turn_id_map[turn.turn_id],
                        parent_turn_id=(
                            turn_id_map[turn.parent_turn_id]
                            if turn.parent_turn_id is not None
                            else None
                        ),
                    )
                    for turn in selected_turns
                ]
                new_branch_root_turn_id = turn_id_map[branch_root_turn_id]
                self._validate_bundle([updated_source], remaining_turns)
                self._validate_bundle([new_case], new_turns)

                by_id = {turn.turn_id: turn for turn in source_turns}

                def depth(turn: TurnRecord) -> int:
                    value = 0
                    parent_id = turn.parent_turn_id
                    while parent_id is not None:
                        value += 1
                        parent_id = by_id[parent_id].parent_turn_id
                    return value

                for turn in sorted(moved_turns, key=depth, reverse=True):
                    connection.execute(
                        "DELETE FROM turns WHERE case_id = ? AND turn_id = ?",
                        (case_id, turn.turn_id),
                    )
                connection.execute(
                    "UPDATE cases SET updated_at = ? WHERE case_id = ?",
                    (split_at, case_id),
                )
                self._insert_case(connection, new_case)
                for turn in new_turns:
                    self._insert_turn(connection, turn)

                _verify_database_shape(connection, expected_signature=_SCHEMA_SIGNATURE)
                if self._get_case(connection, case_id) != updated_source:
                    raise StorageError("branch split source Case read-back mismatch")
                if self._get_turns(connection, case_id) != remaining_turns:
                    raise StorageError("branch split source Turn read-back mismatch")
                if self._get_case(connection, new_case_id) != new_case:
                    raise StorageError("branch split new Case read-back mismatch")
                if self._get_turns(connection, new_case_id) != new_turns:
                    raise StorageError("branch split new Turn read-back mismatch")
                connection.commit()
        except Exception as error:
            recovery_errors: list[str] = []
            if target.exists() and backup_verified:
                try:
                    with sqlite3.connect(target) as backup_read:
                        backup_read.row_factory = sqlite3.Row
                        _verify_database_shape(
                            backup_read, expected_signature=_SCHEMA_SIGNATURE
                        )
                        if source_snapshot is not None and _snapshot(
                            backup_read
                        ) != source_snapshot:
                            raise StorageError("branch split backup verification failed")
                except Exception as backup_error:
                    recovery_errors.append(str(backup_error))
            elif target.exists():
                try:
                    target.unlink()
                except OSError as cleanup_error:
                    recovery_errors.append(
                        f"partial backup cleanup failed: {cleanup_error}"
                    )
            try:
                with self._connect() as rolled_back:
                    _verify_database_shape(
                        rolled_back, expected_signature=_SCHEMA_SIGNATURE
                    )
                    if source_snapshot is not None and _snapshot(
                        rolled_back
                    ) != source_snapshot:
                        raise StorageError("branch split rollback verification failed")
            except Exception as rollback_error:
                recovery_errors.append(str(rollback_error))
            if recovery_errors:
                raise StorageError(
                    "branch split failed and recovery verification failed: "
                    + "; ".join(recovery_errors)
                ) from error
            backup_state = "retained" if backup_verified else "removed"
            raise StorageError(
                "branch split failed; rollback verified; "
                f"backup {backup_state}: {error}"
            ) from error

        if new_case is None or updated_source is None:
            raise StorageError("branch split produced no committed records")
        status = self.status()
        if self.get_case(case_id) != updated_source:
            raise StorageError("branch split committed source Case read-back mismatch")
        if self.get_turns(case_id) != remaining_turns:
            raise StorageError("branch split committed source Turn read-back mismatch")
        if self.get_case(new_case.case_id) != new_case:
            raise StorageError("branch split committed new Case read-back mismatch")
        if self.get_turns(new_case.case_id) != new_turns:
            raise StorageError("branch split committed new Turn read-back mismatch")
        return {
            "operation": "case-split-branch",
            "source_case_id": case_id,
            "new_case_id": new_case.case_id,
            "source_branch_root_turn_id": branch_root_turn_id,
            "new_branch_root_turn_id": new_branch_root_turn_id,
            "copied_ancestor_count": len(ancestors),
            "moved_branch_turn_count": len(moved_turns),
            "source_turn_count": len(remaining_turns),
            "new_turn_count": len(new_turns),
            "database_backup": str(target),
            "backup_verified": True,
            "database_integrity": status["integrity"],
            "committed_read_back": "verified",
        }

    def add_turn(
        self,
        turn: TurnRecord,
        *,
        target_status: CaseStatus,
        updated_at: str,
        reason: str,
        replace_draft_id: str | None,
        approved: bool,
    ) -> Mapping[str, object]:
        """Atomically append one turn, optionally retire a Draft, and update Case state."""
        _require_approval(approved)
        promoted_draft = False
        try:
            with self._connect(writable=True) as connection:
                connection.execute("BEGIN IMMEDIATE")
                case = self._get_case(connection, turn.case_id)
                existing_turns = self._get_turns(connection, turn.case_id)
                stored_turn = replace(
                    turn,
                    turn_id=next_turn_id(item.turn_id for item in existing_turns),
                )
                validate_turn(stored_turn)
                if (turn.post_id, turn.root_comment_id) != (
                    case.post_id,
                    case.root_comment_id,
                ):
                    raise StorageError(f"turn identity conflicts with Case: {turn.turn_id}")
                validate_transition(LifecycleTransition(case.status, target_status, reason))
                if replace_draft_id is not None:
                    draft = next(
                        (item for item in existing_turns if item.turn_id == replace_draft_id),
                        None,
                    )
                    if (
                        draft is None
                        or draft.direction is not TurnDirection.OUTGOING
                        or draft.state is not TurnState.DRAFT
                    ):
                        raise StorageError(
                            f"replacement target is not an Outgoing Draft: {replace_draft_id}"
                        )
                    same_turn_identity = (
                        draft.parent_turn_id == stored_turn.parent_turn_id
                        and draft.direction is stored_turn.direction
                        and draft.exact_text == stored_turn.exact_text
                        and (
                            draft.reply_comment_id is None
                            or stored_turn.reply_comment_id is None
                            or draft.reply_comment_id == stored_turn.reply_comment_id
                        )
                    )
                    if same_turn_identity:
                        stored_turn = replace(
                            stored_turn,
                            turn_id=draft.turn_id,
                            reply_comment_id=(
                                stored_turn.reply_comment_id or draft.reply_comment_id
                            ),
                            exact_url=stored_turn.exact_url or draft.exact_url,
                            url_supplied_at=(
                                stored_turn.url_supplied_at or draft.url_supplied_at
                            ),
                        )
                        validate_turn(stored_turn)
                        duplicate = _find_turn_duplicate(connection, stored_turn)
                        if duplicate is not None and (
                            duplicate.case_id,
                            duplicate.turn_id,
                        ) != (draft.case_id, draft.turn_id):
                            connection.rollback()
                            return _duplicate_turn_receipt(duplicate, stored_turn)
                        graph = [
                            stored_turn if item.turn_id == draft.turn_id else item
                            for item in existing_turns
                        ]
                        validate_parent_graph(graph)
                        values = _turn_values(stored_turn)
                        connection.execute(
                            """UPDATE turns SET
                                parent_turn_id = :parent_turn_id,
                                parent_confidence = :parent_confidence,
                                participant_ref = :participant_ref,
                                direction = :direction,
                                kind = :kind,
                                state = :state,
                                exact_text = :exact_text,
                                post_id = :post_id,
                                root_comment_id = :root_comment_id,
                                reply_comment_id = :reply_comment_id,
                                exact_url = :exact_url,
                                url_supplied_at = :url_supplied_at,
                                observed_at = :observed_at,
                                notes = :notes
                            WHERE case_id = :case_id AND turn_id = :turn_id""",
                            values,
                        )
                        promoted_draft = True
                    else:
                        duplicate = _find_turn_duplicate(connection, stored_turn)
                        if duplicate is not None:
                            connection.rollback()
                            return _duplicate_turn_receipt(duplicate, stored_turn)
                        connection.execute(
                            "UPDATE turns SET state = ? WHERE case_id = ? AND turn_id = ?",
                            (TurnState.REPLACED.value, turn.case_id, replace_draft_id),
                        )
                        existing_turns = [
                            item
                            if item.turn_id != replace_draft_id
                            else replace(item, state=TurnState.REPLACED)
                            for item in existing_turns
                        ]
                        validate_parent_graph([*existing_turns, stored_turn])
                        self._insert_turn(connection, stored_turn)
                else:
                    duplicate = _find_turn_duplicate(connection, stored_turn)
                    if duplicate is not None:
                        connection.rollback()
                        return _duplicate_turn_receipt(duplicate, stored_turn)
                    validate_parent_graph([*existing_turns, stored_turn])
                    self._insert_turn(connection, stored_turn)
                connection.execute(
                    "UPDATE cases SET status = ?, updated_at = ? WHERE case_id = ?",
                    (target_status.value, updated_at, turn.case_id),
                )
                connection.commit()
        except sqlite3.Error as error:
            raise StorageError(f"turn write failed: {error}") from error
        actual_turns = self.get_turns(stored_turn.case_id)
        actual = next(
            (item for item in actual_turns if item.turn_id == stored_turn.turn_id), None
        )
        actual_case = self.get_case(turn.case_id)
        if (
            actual != stored_turn
            or actual_case.status is not target_status
            or actual_case.updated_at != updated_at
        ):
            raise StorageError("turn write read-back mismatch")
        if replace_draft_id is not None and not promoted_draft:
            replaced = next(
                (item for item in actual_turns if item.turn_id == replace_draft_id), None
            )
            if replaced is None or replaced.state is not TurnState.REPLACED:
                raise StorageError("Draft replacement read-back mismatch")
        return {
            "created": not promoted_draft,
            "case_id": turn.case_id,
            "turn_id": stored_turn.turn_id,
            "promoted": promoted_draft,
            "status": actual_case.status.value,
            "readback": "verified",
        }

    def close_case(
        self, updated: CaseRecord, *, reason: str, approved: bool
    ) -> Mapping[str, object]:
        """Atomically write only closure fields and verify the committed Case."""
        _require_approval(approved)
        validate_case(updated)
        try:
            with self._connect(writable=True) as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._get_case(connection, updated.case_id)
                validate_transition(LifecycleTransition(current.status, updated.status, reason))
                connection.execute(
                    """UPDATE cases SET
                        updated_at = :updated_at,
                        status = :status,
                        outcome_score = :outcome_score,
                        outcome_class = :outcome_class,
                        outcome_notes = :outcome_notes,
                        user_rating = :user_rating,
                        what_worked = :what_worked,
                        what_failed = :what_failed,
                        next_test = :next_test,
                        closed_at = :closed_at
                    WHERE case_id = :case_id""",
                    _case_values(updated),
                )
                connection.commit()
        except sqlite3.Error as error:
            raise StorageError(f"case close failed: {error}") from error
        actual = self.get_case(updated.case_id)
        expected = to_jsonable(updated)
        observed = to_jsonable(actual)
        if not isinstance(expected, Mapping) or not isinstance(observed, Mapping):
            raise StorageError("case close read-back serialization failed")
        verification = verify_readback(expected, observed)
        if not verification.succeeded:
            raise StorageError("case close read-back mismatch")
        return {
            "case_id": actual.case_id,
            "status": actual.status.value,
            "readback": "verified",
        }
