"""Installable command-line interface for deterministic HasbaraTops operations."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__
from .enums import CaseStatus, OutcomeClass, ParentConfidence, TurnDirection, TurnKind, TurnState
from .errors import HasbaraTopsError, StorageError, WriteSafetyError
from .facebook_url import parse_facebook_url
from .identifiers import next_case_id, next_turn_id
from .lifecycle import CLOSED_STATUSES, validate_posted_turn, validate_transition
from .models import CaseRecord, LifecycleTransition, TurnRecord, to_jsonable
from .parent_graph import validate_parent_graph
from .readback import verify_readback
from .schema import CASE_FIELDS, SCHEMA_VERSION, TURN_FIELDS
from .source_consistency import file_sha256
from .storage import SQLiteStore
from .validation import validate_case, validate_turn


def _load_json(path: str) -> object:
    with Path(path).open(encoding="utf-8") as stream:
        value: object = json.load(stream)
    return value


def _mapping(value: object, label: str = "JSON document") -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise HasbaraTopsError(f"{label} must be a JSON object")
    return value


def _list(value: object, label: str = "JSON document") -> list[object]:
    if not isinstance(value, list):
        raise HasbaraTopsError(f"{label} must be a JSON list")
    return value


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(str(value))


def _boolean(value: object, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "yes"}:
        return True
    if isinstance(value, str) and value.lower() in {"false", "no"}:
        return False
    raise HasbaraTopsError(f"{label} must be a boolean")


def _reject_unknown(
    payload: Mapping[str, Any], allowed: set[str], label: str
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise HasbaraTopsError(f"unknown {label} fields: {', '.join(unknown)}")


def _case_record(payload: Mapping[str, Any]) -> CaseRecord:
    _reject_unknown(payload, set(CASE_FIELDS), "Case")
    links = payload.get("source_links", [])
    return CaseRecord(
        case_id=str(payload.get("case_id", "")),
        case_title=str(payload.get("case_title", "")),
        created_at=str(payload.get("created_at", "")),
        updated_at=str(payload.get("updated_at", "")),
        status=CaseStatus(str(payload.get("status", ""))),
        topic=str(payload.get("topic", "")),
        post_text=str(payload.get("post_text", "")),
        post_url=str(payload.get("post_url", "")),
        post_id=str(payload.get("post_id", "")),
        root_comment_id=str(payload.get("root_comment_id", "")),
        source_links=tuple(str(item) for item in _list(links, "source_links")),
        privacy_checked=_boolean(payload.get("privacy_checked", False), "privacy_checked"),
        outcome_score=_optional_int(payload.get("outcome_score")),
        outcome_class=(
            OutcomeClass(str(payload["outcome_class"]))
            if payload.get("outcome_class") is not None
            else None
        ),
        outcome_notes=str(payload.get("outcome_notes", "")),
        user_rating=_optional_int(payload.get("user_rating")),
        what_worked=str(payload.get("what_worked", "")),
        what_failed=str(payload.get("what_failed", "")),
        next_test=str(payload.get("next_test", "")),
        closed_at=str(payload.get("closed_at", "")),
    )


def _turn_record(payload: Mapping[str, Any]) -> TurnRecord:
    _reject_unknown(payload, set(TURN_FIELDS), "Turn")
    return TurnRecord(
        case_id=str(payload.get("case_id", "")),
        turn_id=str(payload.get("turn_id", "")),
        parent_turn_id=_optional_string(payload.get("parent_turn_id")),
        parent_confidence=(
            ParentConfidence(str(payload["parent_confidence"]))
            if payload.get("parent_confidence") is not None
            else None
        ),
        participant_ref=str(payload.get("participant_ref", "")),
        direction=TurnDirection(str(payload.get("direction", ""))),
        kind=TurnKind(str(payload.get("kind", ""))),
        state=TurnState(str(payload.get("state", ""))),
        exact_text=str(payload.get("exact_text", "")),
        post_id=str(payload.get("post_id", "")),
        root_comment_id=str(payload.get("root_comment_id", "")),
        reply_comment_id=_optional_string(payload.get("reply_comment_id")),
        exact_url=_optional_string(payload.get("exact_url")),
        url_supplied_at=_optional_string(payload.get("url_supplied_at")),
        observed_at=str(payload.get("observed_at", "")),
        notes=str(payload.get("notes", "")),
    )


def _print(value: object) -> None:
    print(
        json.dumps(
            to_jsonable(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _repo_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise StorageError("run the command from the HasbaraTops repository")


def _outside_repository(path: Path) -> Path:
    resolved = path.resolve()
    root = _repo_root(Path.cwd())
    try:
        resolved.relative_to(root)
    except ValueError:
        return resolved
    raise WriteSafetyError("canonical databases, exports, and backups must stay outside Git")


def _storage_config(root: Path) -> dict[str, Any]:
    config_path = root / "config" / "storage.toml"
    if not config_path.is_file():
        raise StorageError(f"storage config does not exist: {config_path}")
    with config_path.open("rb") as stream:
        return tomllib.load(stream)


def _database_path(args: argparse.Namespace) -> Path:
    config = _storage_config(_repo_root(Path.cwd()))
    environment_variable = str(config["database"]["path_environment_variable"])
    configured = args.database or os.environ.get(environment_variable)
    if not configured:
        raise StorageError(f"set {environment_variable} or pass --database")
    return _outside_repository(Path(str(configured)))


def _store(args: argparse.Namespace) -> SQLiteStore:
    return SQLiteStore(_database_path(args))


def _check(args: argparse.Namespace) -> dict[str, object]:
    root = _repo_root(Path.cwd())
    config_path = root / "config" / "storage.toml"
    config = _storage_config(root)
    documents = config["documents"]
    checks: dict[str, bool] = {
        "storage_config": config_path.is_file(),
        "agents": (root / str(documents["governance"])).is_file(),
        "strategy_guide": (root / str(documents["strategy_guide"])).is_file(),
        "evidence_base": (root / str(documents["evidence_base"])).is_file(),
    }
    configured_version = int(config["database"]["schema_version"])
    checks["configured_schema_version"] = configured_version == SCHEMA_VERSION
    document_revisions = {
        name: file_sha256(root / str(path))
        for name, path in documents.items()
        if (root / str(path)).is_file()
    }
    status = _store(args).status()
    checks["database_integrity"] = status["integrity"] == "ok"
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "database": status,
        "document_revisions": document_revisions,
        "configured_schema_version": configured_version,
        "writer_policy": "transactional; explicit approval; committed read-back",
    }


def _add_database_write_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--approved", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="HasbaraTops",
        description="Deterministic local operations for HasbaraTops",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--database", help="SQLite path; defaults to HASBARATOPS_DB")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "check", help="verify canonical Markdown, configuration, and database integrity"
    )
    init = sub.add_parser("db-init", help="initialize or verify the canonical SQLite database")
    _add_database_write_arguments(init)
    sub.add_parser("db-status", help="report schema, integrity, and row counts")
    backup = sub.add_parser("db-backup", help="create a consistent non-overwriting backup")
    backup.add_argument("--destination", required=True)
    _add_database_write_arguments(backup)

    parse = sub.add_parser("parse-url", help="parse a Facebook URL without rewriting it")
    parse.add_argument("url")
    case_id = sub.add_parser("next-case-id", help="calculate the next global Case ID")
    case_id.add_argument("--existing", required=True)
    turn_id = sub.add_parser("next-turn-id", help="calculate a case-local Turn ID")
    turn_id.add_argument("--case-id", required=True)
    turn_id.add_argument("--existing", required=True)

    validate_case_parser = sub.add_parser("validate-case", help="validate a Case record")
    validate_case_parser.add_argument("json_file")
    validate_turn_parser = sub.add_parser("validate-turn", help="validate a Turn record")
    validate_turn_parser.add_argument("json_file")
    transition = sub.add_parser("validate-transition", help="validate a Case transition")
    transition.add_argument("json_file")
    graph = sub.add_parser("validate-parent-graph", help="validate a case-local parent graph")
    graph.add_argument("json_file")
    readback = sub.add_parser("verify-readback", help="compare expected and actual fields")
    readback.add_argument("--expected", required=True)
    readback.add_argument("--actual", required=True)

    find = sub.add_parser(
        "case-find", help="resolve by definitive Case ID or discover root candidates"
    )
    find.add_argument("--case-id")
    find.add_argument("--post-id")
    find.add_argument("--root-comment-id")
    show = sub.add_parser("case-show", help="return one Case and its complete Turn graph")
    show.add_argument("--case-id", required=True)
    split = sub.add_parser(
        "case-split-branch", help="move one reply branch into the next global Case"
    )
    split.add_argument("--case-id", required=True)
    split.add_argument("--branch-root-turn-id", required=True)
    split.add_argument("--new-case-title", required=True)
    split.add_argument("--new-topic", required=True)
    split.add_argument("--backup-destination", required=True)
    _add_database_write_arguments(split)
    sub.add_parser("case-list-open", help="return latest-Turn links for open Cases")
    sub.add_parser("strategy-dataset", help="return all closed Cases and their Turns")

    intake = sub.add_parser("case-intake", help="create one Case and initial Turns atomically")
    intake.add_argument("json_file")
    _add_database_write_arguments(intake)
    followup = sub.add_parser("case-followup", help="record one incoming Turn atomically")
    followup.add_argument("--case-id", required=True)
    followup.add_argument("json_file")
    _add_database_write_arguments(followup)
    posting = sub.add_parser("case-record-posting", help="record a confirmed posted reply")
    posting.add_argument("--case-id", required=True)
    posting.add_argument("json_file")
    _add_database_write_arguments(posting)
    close = sub.add_parser("case-close", help="close one Case atomically")
    close.add_argument("--case-id", required=True)
    close.add_argument("json_file")
    _add_database_write_arguments(close)
    return parser


def _ids_from_json(value: object, key: str, case_id: str | None = None) -> list[str]:
    items = _list(value)
    output: list[str] = []
    for item in items:
        if isinstance(item, str):
            output.append(item)
        else:
            row = _mapping(item, "existing identifier row")
            if case_id is None or str(row.get("case_id", "")) == case_id:
                output.append(str(row.get(key, "")))
    return output


def _turn_for_case(
    payload: Mapping[str, Any], case: CaseRecord, turn_id: str
) -> TurnRecord:
    values = dict(payload)
    values.pop("draft_turn_id", None)
    values.update(
        {
            "case_id": case.case_id,
            "turn_id": turn_id,
            "post_id": case.post_id,
            "root_comment_id": case.root_comment_id,
        }
    )
    exact_url = _optional_string(values.get("exact_url"))
    if exact_url is not None:
        parsed = parse_facebook_url(exact_url)
        if parsed.reply_comment_id is not None:
            supplied = _optional_string(values.get("reply_comment_id"))
            if supplied is not None and supplied != parsed.reply_comment_id:
                raise HasbaraTopsError(
                    "reply_comment_id conflicts with the supplied Exact URL"
                )
            values["reply_comment_id"] = parsed.reply_comment_id
    return _turn_record(values)


def _run_database_command(args: argparse.Namespace, command: str) -> object:
    store = _store(args)
    if command == "db-init":
        return store.initialize(approved=bool(args.approved))
    if command == "db-status":
        return store.status()
    if command == "db-backup":
        destination = _outside_repository(Path(str(args.destination)))
        return store.backup(destination, approved=bool(args.approved))
    if command == "case-find":
        case_id = _optional_string(args.case_id)
        post_id = _optional_string(args.post_id)
        root_comment_id = _optional_string(args.root_comment_id)
        if case_id is not None:
            if post_id is not None or root_comment_id is not None:
                raise HasbaraTopsError(
                    "case-find accepts either --case-id or a Post ID + Root Comment ID pair"
                )
            case = store.get_case(case_id)
            return {"found": True, "case_id": case.case_id, "status": case.status.value}
        if post_id is None or root_comment_id is None:
            raise HasbaraTopsError(
                "case-find requires --case-id or both --post-id and --root-comment-id"
            )
        candidates = store.find_cases(post_id, root_comment_id)
        return {
            "found": bool(candidates),
            "candidate_count": len(candidates),
            "candidates": [
                {"case_id": case.case_id, "status": case.status.value}
                for case in candidates
            ],
        }
    if command == "case-show":
        case_id = str(args.case_id)
        return {"case": store.get_case(case_id), "turns": store.get_turns(case_id)}
    if command == "case-split-branch":
        destination = _outside_repository(Path(str(args.backup_destination)))
        return store.split_case_branch(
            str(args.case_id),
            str(args.branch_root_turn_id),
            new_case_title=str(args.new_case_title),
            new_topic=str(args.new_topic),
            backup_destination=destination,
            approved=bool(args.approved),
        )
    if command == "case-list-open":
        return store.list_open_summaries()
    if command == "strategy-dataset":
        return store.strategy_dataset()
    if command == "case-intake":
        payload = _mapping(_load_json(str(args.json_file)), "case intake")
        _reject_unknown(payload, {"case", "turns"}, "case intake")
        case_payload = _mapping(payload.get("case"), "case")
        case = _case_record({**case_payload, "case_id": ""})
        initial_turns: list[TurnRecord] = []
        for item in _list(payload.get("turns", []), "turns"):
            initial_turns.append(
                _turn_for_case(_mapping(item, "initial Turn"), case, "")
            )
        return store.create_case(
            case, initial_turns, approved=bool(args.approved), allocate_ids=True
        )
    if command in {"case-followup", "case-record-posting"}:
        case_id = str(args.case_id)
        case = store.get_case(case_id)
        if case.status in CLOSED_STATUSES:
            raise HasbaraTopsError(f"case is closed: {case_id}")
        payload = _mapping(_load_json(str(args.json_file)), "Turn payload")
        _reject_unknown(payload, {*TURN_FIELDS, "draft_turn_id"}, "Turn")
        turn = _turn_for_case(payload, case, "")
        if command == "case-followup":
            if turn.direction is not TurnDirection.INCOMING:
                raise HasbaraTopsError("case-followup requires an Incoming Turn")
            target_status = CaseStatus.ACTIVE_EXCHANGE
            reason = "incoming public turn"
            replace_draft_id = None
        else:
            validate_posted_turn(turn)
            target_status = CaseStatus.POSTED if case.status is CaseStatus.DRAFT else case.status
            reason = "posting confirmed"
            replace_draft_id = _optional_string(payload.get("draft_turn_id"))
        return store.add_turn(
            turn,
            target_status=target_status,
            updated_at=turn.observed_at,
            reason=reason,
            replace_draft_id=replace_draft_id,
            approved=bool(args.approved),
        )
    if command == "case-close":
        case = store.get_case(str(args.case_id))
        payload = _mapping(_load_json(str(args.json_file)), "case close")
        close_fields = {
            "status",
            "updated_at",
            "outcome_score",
            "outcome_class",
            "outcome_notes",
            "user_rating",
            "what_worked",
            "what_failed",
            "next_test",
            "closed_at",
            "reason",
        }
        _reject_unknown(payload, close_fields, "case close")
        status = CaseStatus(str(payload.get("status", "")))
        if status not in CLOSED_STATUSES:
            raise HasbaraTopsError("case-close requires a Closed status")
        required_text = (
            "updated_at",
            "outcome_notes",
            "what_worked",
            "what_failed",
            "next_test",
            "closed_at",
        )
        missing = [name for name in required_text if not str(payload.get(name, "")).strip()]
        if missing or payload.get("outcome_score") is None or payload.get("outcome_class") is None:
            raise HasbaraTopsError("case-close payload lacks required outcome fields")
        updated = replace(
            case,
            updated_at=str(payload["updated_at"]),
            status=status,
            outcome_score=int(str(payload["outcome_score"])),
            outcome_class=OutcomeClass(str(payload["outcome_class"])),
            outcome_notes=str(payload["outcome_notes"]),
            user_rating=_optional_int(payload.get("user_rating")),
            what_worked=str(payload["what_worked"]),
            what_failed=str(payload["what_failed"]),
            next_test=str(payload["next_test"]),
            closed_at=str(payload["closed_at"]),
        )
        return store.close_case(
            updated,
            reason=str(payload.get("reason", "explicit closeout")),
            approved=bool(args.approved),
        )
    raise HasbaraTopsError(f"unknown database command: {command}")


def _run(args: argparse.Namespace) -> object:
    command = str(args.command)
    if command == "check":
        result = _check(args)
        if not result["ok"]:
            raise HasbaraTopsError("local readiness checks failed")
        return result
    database_commands = {
        "db-init",
        "db-status",
        "db-backup",
        "case-find",
        "case-show",
        "case-split-branch",
        "case-list-open",
        "strategy-dataset",
        "case-intake",
        "case-followup",
        "case-record-posting",
        "case-close",
    }
    if command in database_commands:
        return _run_database_command(args, command)
    if command == "parse-url":
        return parse_facebook_url(str(args.url))
    if command == "next-case-id":
        existing = _ids_from_json(_load_json(str(args.existing)), "case_id")
        return {"case_id": next_case_id(existing)}
    if command == "next-turn-id":
        existing = _ids_from_json(
            _load_json(str(args.existing)), "turn_id", str(args.case_id)
        )
        return {"case_id": args.case_id, "turn_id": next_turn_id(existing)}
    if command == "validate-case":
        validate_case(_case_record(_mapping(_load_json(str(args.json_file)))))
        return {"valid": True}
    if command == "validate-turn":
        validate_turn(_turn_record(_mapping(_load_json(str(args.json_file)))))
        return {"valid": True}
    if command == "validate-transition":
        payload = _mapping(_load_json(str(args.json_file)))
        validate_transition(
            LifecycleTransition(
                CaseStatus(str(payload.get("from_status", ""))),
                CaseStatus(str(payload.get("to_status", ""))),
                str(payload.get("reason", "")),
            )
        )
        return {"valid": True}
    if command == "validate-parent-graph":
        turns = [_turn_record(_mapping(item)) for item in _list(_load_json(str(args.json_file)))]
        validate_parent_graph(turns)
        return {"valid": True, "turn_count": len(turns)}
    if command == "verify-readback":
        expected = _mapping(_load_json(str(args.expected)))
        actual = _mapping(_load_json(str(args.actual)))
        verification = verify_readback(expected, actual)
        if not verification.succeeded:
            raise HasbaraTopsError(
                "read-back verification failed: " + "; ".join(verification.mismatches)
            )
        return verification
    raise HasbaraTopsError(f"unknown command: {command}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        _print(_run(args))
        return 0
    except (HasbaraTopsError, OSError, ValueError, KeyError, sqlite3.Error) as error:
        category = (
            "storage_error"
            if isinstance(error, sqlite3.Error)
            else getattr(error, "category", "input_error")
        )
        print(
            json.dumps(
                {"ok": False, "error": str(error), "category": category},
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
