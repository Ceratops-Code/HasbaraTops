"""Typed provider-independent domain records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from .enums import (
    CaseStatus,
    OutcomeClass,
    ParentConfidence,
    TurnDirection,
    TurnKind,
    TurnState,
)


@dataclass(frozen=True)
class ParsedFacebookURL:
    original_url: str
    normalized_host: str | None
    post_id: str | None
    root_comment_id: str | None
    reply_comment_id: str | None
    is_facebook_url: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class SchemaSignature:
    value: str


@dataclass(frozen=True)
class CaseRecord:
    case_id: str
    case_title: str
    created_at: str
    updated_at: str
    status: CaseStatus
    topic: str
    post_text: str
    post_url: str
    post_id: str
    root_comment_id: str
    source_links: tuple[str, ...] = ()
    privacy_checked: bool = False
    outcome_score: int | None = None
    outcome_class: OutcomeClass | None = None
    outcome_notes: str = ""
    user_rating: int | None = None
    what_worked: str = ""
    what_failed: str = ""
    next_test: str = ""
    closed_at: str = ""

@dataclass(frozen=True)
class TurnRecord:
    case_id: str
    turn_id: str
    parent_turn_id: str | None
    parent_confidence: ParentConfidence | None
    participant_ref: str
    direction: TurnDirection
    kind: TurnKind
    state: TurnState
    exact_text: str
    post_id: str
    root_comment_id: str
    reply_comment_id: str | None = None
    exact_url: str | None = None
    url_supplied_at: str | None = None
    observed_at: str = ""
    notes: str = ""


@dataclass(frozen=True)
class LifecycleTransition:
    from_status: CaseStatus
    to_status: CaseStatus
    reason: str


@dataclass(frozen=True)
class WriteVerification:
    succeeded: bool
    expected: Mapping[str, object]
    actual: Mapping[str, object]
    mismatches: tuple[str, ...]


@dataclass(frozen=True)
class ThreadMapEntry:
    turn_id: str
    parent_turn_id: str | None
    participant_ref: str
    excerpt: str
    state: TurnState


def to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses and enums to JSON-compatible values."""
    if hasattr(value, "__dataclass_fields__"):
        return to_jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_jsonable(item) for item in value]
    return value
