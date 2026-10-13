"""Canonical direct-message task intent and raw provenance.

Judges in this module are classifiers only.  The literal text received from an
authoritative user source is persisted before prompt expansion and is never
replaced by model-authored wording.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional


MAX_RAW_TASK_MESSAGES = 32
MAX_TASK_SUPPLEMENTS = 16
MAX_SESSION_LINEAGE = 8

RELATIONSHIP_LABELS = {
    "new_task",
    "same_task",
    "related_question",
    "supplement",
    "replacement",
    "cancellation",
    "unclear",
}
STATE_EFFECTS = {
    "start_new",
    "no_change",
    "append_contract",
    "supersede",
    "cancel",
}
_ALLOWED_RELATIONSHIP_EFFECTS = {
    "new_task": {"start_new"},
    "same_task": {"no_change"},
    "related_question": {"no_change"},
    "supplement": {"append_contract"},
    "replacement": {"supersede"},
    "cancellation": {"cancel"},
    "unclear": {"no_change"},
}
_AUTHORITATIVE_SOURCE_KINDS = {"direct_user", "direct_external_user"}
_FORBIDDEN_REWRITE_KEYS = {
    "summary",
    "summarized_task",
    "rewritten_task",
    "rewrite",
    "normalized_user_request",
    "normalized_text",
    "cleaned_text",
    "paraphrase",
    "paraphrased_text",
    "canonical_text",
    "task_text",
    "updated_task",
}
_ALLOWED_JUDGE_KEYS = {
    "relationship",
    "state_effect",
    "confidence",
    "reason_codes",
    "evidence_quotes",
    "_rejected_rewrite_keys",
    "_dropped_judge_keys",
    "cache",
    "cache_key",
    "latency_ms",
    "error_type",
}
_HIGH_IMPACT_EFFECTS = {"start_new", "supersede", "cancel"}


def _meta_key(session_id: str) -> str:
    return f"task_intent:{session_id}"


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return [str(value)] if str(value) else []


def _bounded(items: Iterable[Any], limit: int) -> list[Any]:
    values = list(items)
    return values[-limit:] if limit > 0 else []


def clamp_raw_text(text: str, max_chars: int) -> str:
    """Return exact text or exact prefix + ``…`` + exact suffix.

    This is for bounded judge/summary views only. Persisted canonical text is
    never passed through this helper.
    """
    raw = str(text or "")
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return ""
    if len(raw) <= limit:
        return raw
    if limit == 1:
        return "…"
    head = limit // 2
    tail = limit - head - 1
    return raw[:head] + "…" + (raw[-tail:] if tail else "")


def validate_judge_payload_no_rewrite(
    payload: Dict[str, Any], *, raw_text: str
) -> Dict[str, Any]:
    """Whitelist annotation fields and retain exact evidence quotes only."""
    if not isinstance(payload, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    rejected: list[str] = []
    dropped: list[str] = []
    for key, value in payload.items():
        name = str(key)
        if name.lower() in _FORBIDDEN_REWRITE_KEYS:
            rejected.append(name)
        elif name in _ALLOWED_JUDGE_KEYS:
            cleaned[name] = value
        else:
            dropped.append(name)

    quotes = _string_list(cleaned.get("evidence_quotes"))
    cleaned["evidence_quotes"] = [quote for quote in quotes if quote and quote in raw_text]
    if rejected:
        cleaned["_rejected_rewrite_keys"] = rejected
    if dropped:
        cleaned["_dropped_judge_keys"] = dropped
    return cleaned


@dataclass
class TaskRelationshipDecision:
    relationship: str = "unclear"
    state_effect: str = "no_change"
    confidence: float = 0.0
    reason_codes: List[str] = field(default_factory=list)
    evidence_quotes: List[str] = field(default_factory=list)
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    fallback_reason: str = ""

    @classmethod
    def from_payload(
        cls,
        payload: Optional[Dict[str, Any]],
        *,
        raw_text: str,
        fallback_reason: str = "",
    ) -> "TaskRelationshipDecision":
        cleaned = validate_judge_payload_no_rewrite(payload or {}, raw_text=raw_text)
        relationship = str(cleaned.get("relationship") or "unclear").strip()
        effect = str(cleaned.get("state_effect") or "no_change").strip()
        confidence = max(0.0, min(1.0, _safe_float(cleaned.get("confidence"), 0.0)))
        reasons = _string_list(cleaned.get("reason_codes"))
        quotes = _string_list(cleaned.get("evidence_quotes"))

        if relationship not in RELATIONSHIP_LABELS or effect not in STATE_EFFECTS:
            reasons.append("invalid_relationship_schema")
            relationship, effect, confidence = "unclear", "no_change", 0.0
        elif effect not in _ALLOWED_RELATIONSHIP_EFFECTS[relationship]:
            reasons.append("invalid_relationship_effect")
            relationship, effect, confidence = "unclear", "no_change", 0.0

        if effect in _HIGH_IMPACT_EFFECTS:
            if confidence < 0.85:
                reasons.append("low_confidence_high_impact_downgrade")
                relationship, effect = "unclear", "no_change"
                fallback_reason = fallback_reason or "high-impact task mutation lacked confidence"
            elif not quotes:
                reasons.append("high_impact_missing_exact_evidence")
                relationship, effect = "unclear", "no_change"
                fallback_reason = fallback_reason or "high-impact task mutation lacked exact evidence"
        elif effect == "append_contract" and confidence < 0.55:
            reasons.append("low_confidence_supplement_downgrade")
            relationship, effect = "unclear", "no_change"
            fallback_reason = fallback_reason or "supplement classification lacked confidence"

        return cls(
            relationship=relationship,
            state_effect=effect,
            confidence=confidence,
            reason_codes=reasons,
            evidence_quotes=quotes,
            raw_payload=cleaned,
            fallback_reason=fallback_reason,
        )


@dataclass
class RawTaskMessage:
    """Literal provenance record whose ``raw_text`` is always literal input."""

    raw_text: str
    source_kind: str
    relationship_to_active_task: str
    state_effect: str
    created_at: float
    id: str
    source_id: str = ""
    message_id: str = ""
    relationship_confidence: float = 0.0
    judge_result: Dict[str, Any] = field(default_factory=dict)
    machine_origin: str = ""
    machine_status: str = ""

    @classmethod
    def create(
        cls,
        raw_text: str,
        *,
        source_kind: str,
        relationship: str,
        state_effect: str,
        source_id: str = "",
        message_id: str = "",
        confidence: float = 0.0,
        judge_result: Optional[Dict[str, Any]] = None,
        machine_origin: str = "",
        machine_status: str = "",
    ) -> "RawTaskMessage":
        return cls(
            raw_text=str(raw_text or ""),
            source_kind=str(source_kind or "unknown"),
            relationship_to_active_task=str(relationship or "unclear"),
            state_effect=str(state_effect or "no_change"),
            created_at=_now(),
            id=_new_id("raw"),
            source_id=str(source_id or ""),
            message_id=str(message_id or ""),
            relationship_confidence=max(0.0, min(1.0, _safe_float(confidence, 0.0))),
            judge_result=dict(judge_result or {}),
            machine_origin=str(machine_origin or ""),
            machine_status=str(machine_status or ""),
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RawTaskMessage":
        value = data or {}
        source_kind = str(value.get("source_kind") or "unknown")
        return cls(
            raw_text=str(value.get("raw_text") or ""),
            source_kind=source_kind,
            relationship_to_active_task=str(
                value.get("relationship_to_active_task") or "new_task"
            ),
            state_effect=str(value.get("state_effect") or "no_change"),
            created_at=_safe_float(value.get("created_at"), 0.0),
            id=str(value.get("id") or _new_id("raw")),
            source_id=str(value.get("source_id") or ""),
            message_id=str(value.get("message_id") or ""),
            relationship_confidence=max(
                0.0, min(1.0, _safe_float(value.get("relationship_confidence"), 0.0))
            ),
            judge_result=(
                dict(value.get("judge_result") or {})
                if isinstance(value.get("judge_result"), dict)
                else {}
            ),
            machine_origin=str(value.get("machine_origin") or ""),
            machine_status=str(value.get("machine_status") or ""),
        )


@dataclass
class TaskContract:
    raw_primary_text: str
    raw_supplements: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskContract":
        value = data or {}
        return cls(
            raw_primary_text=str(value.get("raw_primary_text") or ""),
            raw_supplements=[str(item) for item in value.get("raw_supplements") or []],
        )


@dataclass
class TaskIntentState:
    id: str
    created_at: float
    updated_at: float
    status: str
    task_contract: TaskContract
    raw_messages: List[RawTaskMessage] = field(default_factory=list)
    relationship_to_active_task: str = "new_task"
    transition: Dict[str, Any] = field(default_factory=dict)
    session_lineage: List[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "TaskIntentState":
        data = json.loads(raw)
        contract = TaskContract.from_dict(data.get("task_contract") or {})
        raw_messages = [
            RawTaskMessage.from_dict(item)
            for item in data.get("raw_messages") or []
            if isinstance(item, dict)
        ]
        return cls(
            id=str(data.get("id") or _new_id("task")),
            created_at=_safe_float(data.get("created_at"), 0.0),
            updated_at=_safe_float(data.get("updated_at"), 0.0),
            status=str(data.get("status") or "active"),
            task_contract=contract,
            raw_messages=raw_messages,
            relationship_to_active_task=str(
                data.get("relationship_to_active_task") or "new_task"
            ),
            transition=(
                dict(data.get("transition") or {})
                if isinstance(data.get("transition"), dict)
                else {}
            ),
            session_lineage=[str(item) for item in data.get("session_lineage") or []],
        )
