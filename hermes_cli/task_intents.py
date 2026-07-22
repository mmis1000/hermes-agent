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


_DB_CACHE: Dict[str, Any] = {}

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
    """Bounded provenance record whose ``raw_text`` is always literal input."""

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
        # ``source`` is the legacy field from the pre-rebase state shape.
        source_kind = str(value.get("source_kind") or value.get("source") or "direct_user")
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
        # Legacy state stored its primary text at the top level as well.
        if not contract.raw_primary_text and data.get("raw_text"):
            contract.raw_primary_text = str(data.get("raw_text") or "")
        raw_messages = [
            RawTaskMessage.from_dict(item)
            for item in data.get("raw_messages") or []
            if isinstance(item, dict)
        ]
        if not raw_messages and contract.raw_primary_text:
            raw_messages.append(
                RawTaskMessage.create(
                    contract.raw_primary_text,
                    source_kind="direct_user",
                    relationship="new_task",
                    state_effect="start_new",
                )
            )
        return cls(
            id=str(data.get("id") or _new_id("task")),
            created_at=_safe_float(data.get("created_at"), 0.0),
            updated_at=_safe_float(data.get("updated_at"), 0.0),
            status=str(data.get("status") or "active"),
            task_contract=contract,
            raw_messages=_bounded(raw_messages, MAX_RAW_TASK_MESSAGES),
            relationship_to_active_task=str(
                data.get("relationship_to_active_task") or "new_task"
            ),
            transition=(
                dict(data.get("transition") or {})
                if isinstance(data.get("transition"), dict)
                else {}
            ),
            session_lineage=_bounded(
                [str(item) for item in data.get("session_lineage") or []],
                MAX_SESSION_LINEAGE,
            ),
        )


def _get_session_db() -> Optional[Any]:
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB

        home_path = get_hermes_home()
        home = str(home_path)
    except Exception:
        return None
    cached = _DB_CACHE.get(home)
    if cached is not None:
        return cached
    try:
        # SessionDB's default path is computed when hermes_state is imported.
        # Gateway tests and multi-profile runtimes can change HERMES_HOME later,
        # so pass the current resolved path explicitly instead of leaking state
        # through that import-time default.
        db = SessionDB(home_path / "state.db")
    except Exception:
        return None
    _DB_CACHE[home] = db
    return db


def load_task_intent(session_id: str, *, db: Any = None) -> Optional[TaskIntentState]:
    if not session_id:
        return None
    database = db or _get_session_db()
    if database is None:
        return None
    try:
        raw = database.get_meta(_meta_key(session_id))
    except Exception:
        return None
    if not raw:
        return None
    try:
        return TaskIntentState.from_json(raw)
    except Exception:
        return None


def save_task_intent(session_id: str, state: TaskIntentState, *, db: Any = None) -> bool:
    if not session_id:
        return False
    database = db or _get_session_db()
    if database is None:
        return False
    try:
        database.set_meta(_meta_key(session_id), state.to_json())
    except Exception:
        return False
    return True


def migrate_task_intent_to_session(
    old_session_id: str,
    new_session_id: str,
    *,
    db: Any = None,
) -> bool:
    """Copy canonical active intent to a compression child session."""
    if not old_session_id or not new_session_id or old_session_id == new_session_id:
        return False
    database = db or _get_session_db()
    if database is None or load_task_intent(new_session_id, db=database) is not None:
        return False
    state = load_task_intent(old_session_id, db=database)
    if state is None:
        return False
    state.session_lineage = _bounded(
        [*state.session_lineage, old_session_id], MAX_SESSION_LINEAGE
    )
    state.updated_at = _now()
    return save_task_intent(new_session_id, state, db=database)


class TaskIntentManager:
    """Persisted first-class intent state for one direct-message session."""

    def __init__(self, session_id: str, *, db: Any = None):
        self.session_id = session_id
        self._db = db
        self._state = load_task_intent(session_id, db=db)

    @property
    def state(self) -> Optional[TaskIntentState]:
        return self._state

    def classify_relationship(
        self,
        raw_text: str,
        *,
        source_kind: str,
        relationship_decision: Optional[Dict[str, Any] | TaskRelationshipDecision],
    ) -> TaskRelationshipDecision:
        if source_kind not in _AUTHORITATIVE_SOURCE_KINDS:
            return TaskRelationshipDecision(
                relationship="unclear",
                state_effect="no_change",
                confidence=0.0,
                reason_codes=["non_authoritative_source"],
                fallback_reason="source cannot mutate canonical task intent",
            )
        if self._state is None or self._state.status != "active":
            return TaskRelationshipDecision(
                relationship="new_task",
                state_effect="start_new",
                confidence=1.0,
                reason_codes=["no_active_task"],
                evidence_quotes=[raw_text] if raw_text else [],
            )
        if isinstance(relationship_decision, TaskRelationshipDecision):
            payload = {
                **relationship_decision.raw_payload,
                "relationship": relationship_decision.relationship,
                "state_effect": relationship_decision.state_effect,
                "confidence": relationship_decision.confidence,
                "reason_codes": relationship_decision.reason_codes,
                "evidence_quotes": relationship_decision.evidence_quotes,
            }
            return TaskRelationshipDecision.from_payload(
                payload,
                raw_text=raw_text,
                fallback_reason=relationship_decision.fallback_reason,
            )
        if isinstance(relationship_decision, dict):
            return TaskRelationshipDecision.from_payload(
                relationship_decision, raw_text=raw_text
            )
        return TaskRelationshipDecision(
            relationship="unclear",
            state_effect="no_change",
            confidence=0.0,
            reason_codes=["no_structured_relationship_decision"],
            fallback_reason="bounded relationship judge supplied no decision",
        )

    @staticmethod
    def _judge_result(decision: TaskRelationshipDecision) -> Dict[str, Any]:
        return {
            "relationship": decision.relationship,
            "state_effect": decision.state_effect,
            "confidence": decision.confidence,
            "reason_codes": list(decision.reason_codes),
            "evidence_quotes": list(decision.evidence_quotes),
            "fallback_reason": decision.fallback_reason,
            "rejected_rewrite_keys": list(
                decision.raw_payload.get("_rejected_rewrite_keys") or []
            ),
            "dropped_judge_keys": list(
                decision.raw_payload.get("_dropped_judge_keys") or []
            ),
        }

    def _append_provenance(self, item: RawTaskMessage) -> None:
        assert self._state is not None
        self._state.raw_messages = _bounded(
            [*self._state.raw_messages, item], MAX_RAW_TASK_MESSAGES
        )

    def record_direct_message(
        self,
        raw_text: str,
        *,
        source_kind: str = "direct_user",
        source_id: str = "",
        message_id: str = "",
        relationship_decision: Optional[Dict[str, Any] | TaskRelationshipDecision] = None,
    ) -> Optional[TaskIntentState]:
        raw = str(raw_text or "")
        decision = self.classify_relationship(
            raw,
            source_kind=source_kind,
            relationship_decision=relationship_decision,
        )
        judge_result = self._judge_result(decision)
        provenance = RawTaskMessage.create(
            raw,
            source_kind=source_kind,
            relationship=decision.relationship,
            state_effect=decision.state_effect,
            source_id=source_id,
            message_id=message_id,
            confidence=decision.confidence,
            judge_result=judge_result,
        )

        if source_kind not in _AUTHORITATIVE_SOURCE_KINDS:
            if self._state is None:
                return None
            self._append_provenance(provenance)
            self._state.updated_at = _now()
            save_task_intent(self.session_id, self._state, db=self._db)
            return self._state

        now = _now()
        previous = self._state if self._state and self._state.status == "active" else None
        if previous is None or decision.state_effect in {"start_new", "supersede"}:
            transition: Dict[str, Any] = {}
            if previous is not None:
                transition = {
                    "relationship": decision.relationship,
                    "previous_task_id": previous.id,
                    "previous_raw_primary_text": previous.task_contract.raw_primary_text,
                }
            self._state = TaskIntentState(
                id=_new_id("task"),
                created_at=now,
                updated_at=now,
                status="active",
                task_contract=TaskContract(raw_primary_text=raw),
                raw_messages=[provenance],
                relationship_to_active_task=decision.relationship,
                transition=transition,
                session_lineage=list(previous.session_lineage) if previous else [],
            )
        else:
            assert self._state is not None
            self._append_provenance(provenance)
            if decision.state_effect == "append_contract" and raw:
                supplements = self._state.task_contract.raw_supplements
                if raw not in supplements:
                    self._state.task_contract.raw_supplements = _bounded(
                        [*supplements, raw], MAX_TASK_SUPPLEMENTS
                    )
            elif decision.state_effect == "cancel":
                self._state.status = "cancelled"
                self._state.transition = {
                    "relationship": "cancellation",
                    "cancellation_raw_text": raw,
                }
            self._state.relationship_to_active_task = decision.relationship
            self._state.updated_at = now

        save_task_intent(self.session_id, self._state, db=self._db)
        return self._state

    def record_machine_continuation(
        self,
        raw_text: str,
        *,
        origin: str,
        message_id: str = "",
        status: str = "queued",
    ) -> Optional[TaskIntentState]:
        """Attach machine provenance without creating or mutating user intent."""
        if self._state is None or self._state.status != "active":
            return None
        self._append_provenance(
            RawTaskMessage.create(
                raw_text,
                source_kind="machine_continuation",
                relationship="same_task",
                state_effect="no_change",
                message_id=message_id,
                confidence=1.0,
                judge_result={"reason_codes": ["explicit_machine_continuation"]},
                machine_origin=origin,
                machine_status=status,
            )
        )
        self._state.updated_at = _now()
        save_task_intent(self.session_id, self._state, db=self._db)
        return self._state


__all__ = [
    "MAX_RAW_TASK_MESSAGES",
    "MAX_TASK_SUPPLEMENTS",
    "RELATIONSHIP_LABELS",
    "STATE_EFFECTS",
    "RawTaskMessage",
    "TaskContract",
    "TaskIntentManager",
    "TaskIntentState",
    "TaskRelationshipDecision",
    "clamp_raw_text",
    "load_task_intent",
    "migrate_task_intent_to_session",
    "save_task_intent",
    "validate_judge_payload_no_rewrite",
]
