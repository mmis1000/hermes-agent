"""First-class task intent state for direct user messages and continuations.

This module intentionally keeps raw user / machine text canonical. Parsed
metadata is advisory only; display, relevance checks, and completion guards can
always recover the literal wording that carried task scope.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


_DB_CACHE: Dict[str, Any] = {}

_MULTIPLICITY_TERMS = (
    "more",
    "additional",
    "several",
    "multiple",
    "various",
    "variants",
    "variant",
    "styles",
    "scenes",
    "examples",
    "outputs",
    "comparisons",
    "continue",
    "keep going",
    "until",
    "all",
)

_ONE_SLICE_PATTERNS = (
    re.compile(r"\b(one|1|single|a)\s+(new\s+)?(style|variant|scene|example|output|comparison)\b", re.I),
    re.compile(r"\badded\s+(one|1|a|single)\b", re.I),
    re.compile(r"\bcreated\s+(one|1|a|single)\b", re.I),
)

RELATIONSHIP_LABELS = {
    "same_task",
    "related_question",
    "supplement",
    "possible_new_task",
    "new_task",
    "replacement",
    "unclear",
}

STATE_EFFECTS = {
    "no_change",
    "related_only",
    "append_contract",
    "create_candidate",
    "pause_and_start",
    "supersede",
}

_ALLOWED_RELATIONSHIP_EFFECTS = {
    "same_task": {"no_change", "related_only"},
    "related_question": {"no_change", "related_only"},
    "supplement": {"append_contract"},
    "possible_new_task": {"create_candidate", "no_change"},
    "new_task": {"pause_and_start", "no_change"},
    "replacement": {"supersede"},
    "unclear": {"no_change", "create_candidate"},
}

_AUTHORITATIVE_SOURCE_KINDS = {
    "direct_user",
    "direct_external_user",
    "structured_command",
    "system_event",
}

_FORBIDDEN_JUDGE_REWRITE_KEYS = {
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
}


def _meta_key(session_id: str) -> str:
    return f"task_intent:{session_id}"


def _get_session_db() -> Optional[Any]:
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB

        home = str(get_hermes_home())
    except Exception:
        return None
    cached = _DB_CACHE.get(home)
    if cached is not None:
        return cached
    try:
        db = SessionDB()
    except Exception:
        return None
    _DB_CACHE[home] = db
    return db


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _norm(text: str) -> str:
    return str(text or "").strip().lower()


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return [str(value)] if str(value) else []


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_relationship_effect(relationship: str, state_effect: str) -> tuple[str, str, str]:
    relationship = relationship if relationship in RELATIONSHIP_LABELS else "unclear"
    state_effect = state_effect if state_effect in STATE_EFFECTS else "no_change"
    allowed = _ALLOWED_RELATIONSHIP_EFFECTS.get(relationship, {"no_change"})
    if state_effect in allowed:
        return relationship, state_effect, ""
    return "unclear", "no_change", f"invalid_relationship_effect:{relationship}/{state_effect}"


def clamp_raw_text(text: str, max_chars: int) -> str:
    """Clamp raw text by exact prefix + ellipsis + exact suffix only.

    This helper is intentionally mechanical. It does not normalize, summarize,
    or otherwise rewrite wording; the ellipsis marks the only lost middle span.
    """
    raw = str(text or "")
    try:
        limit = int(max_chars)
    except Exception:
        limit = 0
    if limit <= 0:
        return ""
    if len(raw) <= limit:
        return raw
    if limit == 1:
        return "…"
    head = max(0, limit // 2)
    tail = max(0, limit - head - 1)
    return raw[:head] + "…" + (raw[-tail:] if tail else "")


def validate_judge_payload_no_rewrite(
    payload: Dict[str, Any],
    *,
    raw_texts: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Strip judge output down to annotation-only fields.

    Relationship/completion judges may classify and cite exact evidence. They
    must not supply substitute wording for the task. Any quote field that is not
    an exact substring of the provided raw text is dropped.
    """
    if not isinstance(payload, dict):
        return {}

    cleaned: Dict[str, Any] = {}
    rejected: List[str] = []
    dropped: List[str] = []
    allowed_keys = {
        "relationship",
        "state_effect",
        "confidence",
        "reason_codes",
        "evidence_quotes",
        "_rejected_rewrite_keys",
        "_dropped_judge_keys",
    }
    for key, value in payload.items():
        key_str = str(key)
        if key_str.lower() in _FORBIDDEN_JUDGE_REWRITE_KEYS:
            rejected.append(key_str)
            continue
        if key_str not in allowed_keys:
            dropped.append(key_str)
            continue
        cleaned[key_str] = value

    raw_haystack = "\n".join(str(t or "") for t in (raw_texts or []))
    quotes = _string_list(cleaned.get("evidence_quotes"))
    if quotes:
        cleaned["evidence_quotes"] = [
            quote
            for quote in quotes
            if quote and quote in raw_haystack
        ]

    if rejected:
        cleaned["_rejected_rewrite_keys"] = rejected
    if dropped:
        cleaned["_dropped_judge_keys"] = dropped
    return cleaned


def _multiplicity_terms(text: str) -> List[str]:
    n = _norm(text)
    found: List[str] = []
    for term in _MULTIPLICITY_TERMS:
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", n):
            found.append(term)
    return found


def _looks_one_slice(text: str) -> bool:
    return any(p.search(str(text or "")) for p in _ONE_SLICE_PATTERNS)


def derive_contract_metadata(*texts: str) -> Dict[str, Any]:
    combined = "\n".join(t for t in texts if t)
    terms = _multiplicity_terms(combined)
    return {
        "multiplicity_required": bool(terms),
        "multiplicity_terms": terms,
        # Compatibility key retained for persisted consumers. Natural-language
        # prefix matching is intentionally not used for scope classification;
        # relationship judges may annotate replacements as structured metadata.
        "explicit_scope_reduction": False,
    }


def should_veto_done_for_multiplicity(
    *,
    raw_task_text: str = "",
    response: str = "",
    raw_machine_texts: Optional[List[str]] = None,
) -> bool:
    """Return True when a DONE claim satisfies only one slice of plural work."""
    texts = [raw_task_text or ""] + list(raw_machine_texts or [])
    metadata = derive_contract_metadata(*texts)
    if not metadata.get("multiplicity_required"):
        return False
    if metadata.get("explicit_scope_reduction"):
        return False
    if not re.search(r"\b(done|completed|complete|finished)\b", str(response or ""), re.I):
        return False
    return _looks_one_slice(response)


@dataclass
class TaskContract:
    raw_primary_text: str
    raw_supplements: List[str] = field(default_factory=list)
    derived_metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskContract":
        return cls(
            raw_primary_text=str((data or {}).get("raw_primary_text") or ""),
            raw_supplements=[str(x) for x in ((data or {}).get("raw_supplements") or [])],
            derived_metadata=dict((data or {}).get("derived_metadata") or {}),
        )

    def refresh_metadata(self) -> None:
        self.derived_metadata = derive_contract_metadata(self.raw_primary_text, *self.raw_supplements)


@dataclass
class PreservedMachineMessage:
    raw_text: str
    origin: str = "other"
    status: str = "queued"
    created_at: float = 0.0
    id: str = ""

    @classmethod
    def create(cls, raw_text: str, origin: str = "other", status: str = "queued") -> "PreservedMachineMessage":
        return cls(raw_text=str(raw_text or ""), origin=origin, status=status, created_at=_now(), id=_new_id("machine"))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PreservedMachineMessage":
        return cls(
            raw_text=str((data or {}).get("raw_text") or ""),
            origin=str((data or {}).get("origin") or "other"),
            status=str((data or {}).get("status") or "queued"),
            created_at=float((data or {}).get("created_at") or 0.0),
            id=str((data or {}).get("id") or _new_id("machine")),
        )


@dataclass
class TaskRelationshipDecision:
    """Structured, non-rewriting annotation for one inbound task message.

    The decision is advisory metadata. Canonical task text always comes from
    the raw direct-user message, never from judge-authored prose.
    """

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
        raw_texts: Optional[List[str]] = None,
        fallback_reason: str = "",
    ) -> "TaskRelationshipDecision":
        cleaned = validate_judge_payload_no_rewrite(payload or {}, raw_texts=raw_texts)
        relationship = str(cleaned.get("relationship") or "unclear").strip()
        state_effect = str(cleaned.get("state_effect") or "no_change").strip()
        relationship, state_effect, schema_warning = _normalize_relationship_effect(relationship, state_effect)
        confidence = max(0.0, min(_safe_float(cleaned.get("confidence"), 0.0), 1.0))
        reason_codes = _string_list(cleaned.get("reason_codes"))
        if schema_warning:
            reason_codes.append(schema_warning)
        evidence_quotes = _string_list(cleaned.get("evidence_quotes"))
        return cls(
            relationship=relationship,
            state_effect=state_effect,
            confidence=confidence,
            reason_codes=reason_codes,
            evidence_quotes=evidence_quotes,
            raw_payload=cleaned,
            fallback_reason=fallback_reason,
        )


@dataclass
class RawTaskMessage:
    raw_text: str
    source: str = "user"
    relationship_to_active_task: str = "new_task"
    created_at: float = 0.0
    id: str = ""
    source_kind: str = "direct_user"
    message_id: str = ""
    state_effect: str = "no_change"
    relationship_confidence: float = 0.0
    judge_result: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        raw_text: str,
        *,
        source: str = "user",
        relationship_to_active_task: str = "new_task",
        source_kind: str = "direct_user",
        message_id: str = "",
        state_effect: str = "no_change",
        relationship_confidence: float = 0.0,
        judge_result: Optional[Dict[str, Any]] = None,
    ) -> "RawTaskMessage":
        return cls(
            raw_text=str(raw_text or ""),
            source=source,
            relationship_to_active_task=relationship_to_active_task,
            created_at=_now(),
            id=_new_id("raw"),
            source_kind=source_kind,
            message_id=str(message_id or ""),
            state_effect=state_effect,
            relationship_confidence=float(relationship_confidence or 0.0),
            judge_result=dict(judge_result or {}),
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RawTaskMessage":
        return cls(
            raw_text=str((data or {}).get("raw_text") or ""),
            source=str((data or {}).get("source") or "user"),
            relationship_to_active_task=str((data or {}).get("relationship_to_active_task") or "new_task"),
            created_at=_safe_float((data or {}).get("created_at"), 0.0),
            id=str((data or {}).get("id") or _new_id("raw")),
            source_kind=str((data or {}).get("source_kind") or "direct_user"),
            message_id=str((data or {}).get("message_id") or ""),
            state_effect=str((data or {}).get("state_effect") or "no_change"),
            relationship_confidence=_safe_float((data or {}).get("relationship_confidence"), 0.0),
            judge_result=dict((data or {}).get("judge_result") or {}) if isinstance((data or {}).get("judge_result"), dict) else {},
        )


@dataclass
class TaskIntentState:
    id: str
    kind: str
    raw_text: str
    created_at: float
    updated_at: float
    status: str
    relationship_to_active_task: str
    task_contract: TaskContract
    completion_state: Dict[str, Any] = field(default_factory=dict)
    machine_preserved_messages: List[PreservedMachineMessage] = field(default_factory=list)
    raw_messages: List[RawTaskMessage] = field(default_factory=list)
    last_relevance_check: Optional[Dict[str, Any]] = None

    def to_json(self) -> str:
        data = asdict(self)
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "TaskIntentState":
        data = json.loads(raw)
        contract = TaskContract.from_dict(data.get("task_contract") or {})
        machine = [
            PreservedMachineMessage.from_dict(item)
            for item in (data.get("machine_preserved_messages") or [])
            if isinstance(item, dict)
        ]
        raw_messages = [
            RawTaskMessage.from_dict(item)
            for item in (data.get("raw_messages") or [])
            if isinstance(item, dict)
        ]
        if not raw_messages:
            if contract.raw_primary_text:
                raw_messages.append(
                    RawTaskMessage.create(
                        contract.raw_primary_text,
                        source="user",
                        relationship_to_active_task="legacy_primary",
                    )
                )
            for supplement in contract.raw_supplements:
                raw_messages.append(
                    RawTaskMessage.create(
                        supplement,
                        source="user",
                        relationship_to_active_task="legacy_supplement",
                    )
                )
        return cls(
            id=str(data.get("id") or _new_id("task")),
            kind=str(data.get("kind") or "direct_user_message"),
            raw_text=str(data.get("raw_text") or ""),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            status=str(data.get("status") or "active"),
            relationship_to_active_task=str(data.get("relationship_to_active_task") or "new_task"),
            task_contract=contract,
            completion_state=dict(data.get("completion_state") or {}),
            machine_preserved_messages=machine,
            raw_messages=raw_messages,
            last_relevance_check=data.get("last_relevance_check"),
        )


def load_task_intent(session_id: str) -> Optional[TaskIntentState]:
    if not session_id:
        return None
    db = _get_session_db()
    if db is None:
        return None
    try:
        raw = db.get_meta(_meta_key(session_id))
    except Exception:
        return None
    if not raw:
        return None
    try:
        return TaskIntentState.from_json(raw)
    except Exception:
        return None


def save_task_intent(session_id: str, state: TaskIntentState) -> None:
    if not session_id:
        return
    db = _get_session_db()
    if db is None:
        return
    try:
        db.set_meta(_meta_key(session_id), state.to_json())
    except Exception:
        pass


class TaskIntentManager:
    """Persisted first-class task state for normal/direct messages."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._state = load_task_intent(session_id)

    @property
    def state(self) -> Optional[TaskIntentState]:
        return self._state

    def classify_relationship(
        self,
        text: str,
        *,
        relationship_decision: Optional[Dict[str, Any] | TaskRelationshipDecision] = None,
        source_kind: str = "direct_user",
    ) -> TaskRelationshipDecision:
        """Classify the latest message without natural-language phrase rules.

        This method is intentionally conservative. It accepts structured judge
        annotations, but never infers replacement/supplement from raw text
        prefixes. If there is an active task and no valid structured decision,
        the safe fallback is ``unclear/no_change``.
        """
        if source_kind not in _AUTHORITATIVE_SOURCE_KINDS:
            return TaskRelationshipDecision(
                relationship="unclear",
                state_effect="no_change",
                confidence=0.0,
                reason_codes=["non_authoritative_source"],
                fallback_reason="source cannot mutate task intent",
            )

        if self._state is None or self._state.status in {"completed", "discarded", "superseded"}:
            return TaskRelationshipDecision(
                relationship="new_task",
                state_effect="no_change",
                confidence=1.0,
                reason_codes=["no_active_task"],
            )

        if isinstance(relationship_decision, TaskRelationshipDecision):
            decision = TaskRelationshipDecision.from_payload(
                {
                    **(relationship_decision.raw_payload or {}),
                    "relationship": relationship_decision.relationship,
                    "state_effect": relationship_decision.state_effect,
                    "confidence": relationship_decision.confidence,
                    "reason_codes": _string_list(relationship_decision.reason_codes),
                    "evidence_quotes": _string_list(relationship_decision.evidence_quotes),
                },
                raw_texts=[
                    self._state.task_contract.raw_primary_text,
                    *self._state.task_contract.raw_supplements,
                    str(text or ""),
                ],
                fallback_reason=relationship_decision.fallback_reason,
            )
        elif isinstance(relationship_decision, dict):
            decision = TaskRelationshipDecision.from_payload(
                relationship_decision,
                raw_texts=[
                    self._state.task_contract.raw_primary_text,
                    *self._state.task_contract.raw_supplements,
                    str(text or ""),
                ],
            )
        else:
            decision = TaskRelationshipDecision(
                relationship="unclear",
                state_effect="no_change",
                confidence=0.0,
                reason_codes=["no_structured_relationship_decision"],
                fallback_reason="no bounded judge decision supplied",
            )

        high_impact = decision.state_effect in {"pause_and_start", "supersede"} or decision.relationship in {"new_task", "replacement"}
        if high_impact and decision.confidence < 0.85:
            decision.relationship = "unclear"
            decision.state_effect = "no_change"
            decision.reason_codes.append("low_confidence_high_impact_downgrade")
            decision.fallback_reason = decision.fallback_reason or "high-impact task mutation lacked confidence"
        return decision

    def record_direct_message(
        self,
        raw_text: str,
        *,
        source_kind: str = "direct_user",
        message_id: str = "",
        relationship_decision: Optional[Dict[str, Any] | TaskRelationshipDecision] = None,
    ) -> TaskIntentState:
        text = str(raw_text or "")
        decision = self.classify_relationship(
            text,
            relationship_decision=relationship_decision,
            source_kind=source_kind,
        )
        relationship = decision.relationship
        effect = decision.state_effect
        previous = self._state
        now = _now()
        judge_result = {
            "relationship": decision.relationship,
            "state_effect": decision.state_effect,
            "confidence": decision.confidence,
            "reason_codes": list(decision.reason_codes),
            "evidence_quotes": list(decision.evidence_quotes),
            "fallback_reason": decision.fallback_reason,
            "rejected_rewrite_keys": list((decision.raw_payload or {}).get("_rejected_rewrite_keys") or []),
        }

        if (previous is None or previous.status != "active") and source_kind not in _AUTHORITATIVE_SOURCE_KINDS:
            contract = TaskContract(raw_primary_text="", raw_supplements=[], derived_metadata={})
            contract.refresh_metadata()
            return TaskIntentState(
                id=_new_id("ignored_task"),
                kind="ignored_non_authoritative_message",
                raw_text=text,
                created_at=now,
                updated_at=now,
                status="discarded",
                relationship_to_active_task=relationship,
                task_contract=contract,
                completion_state={"last_verdict": "ignored"},
                raw_messages=[
                    RawTaskMessage.create(
                        text,
                        source="non_authoritative",
                        relationship_to_active_task=relationship,
                        source_kind=source_kind,
                        message_id=message_id,
                        state_effect=effect,
                        relationship_confidence=decision.confidence,
                        judge_result=judge_result,
                    )
                ],
                last_relevance_check={
                    "verdict": "ignored",
                    "reason": "non-authoritative source cannot create task intent",
                    "new_raw_text": text,
                },
            )

        active_previous = previous if previous and previous.status == "active" else None

        if active_previous and effect in {"append_contract", "related_only", "no_change", "create_candidate"}:
            contract = active_previous.task_contract
            if effect == "append_contract" and text and text not in contract.raw_supplements:
                contract.raw_supplements.append(text)
            contract.refresh_metadata()
            raw_source = "user" if source_kind in {"direct_user", "direct_external_user"} else source_kind
            active_previous.raw_messages.append(
                RawTaskMessage.create(
                    text,
                    source=raw_source,
                    relationship_to_active_task=relationship,
                    source_kind=source_kind,
                    message_id=message_id,
                    state_effect=effect,
                    relationship_confidence=decision.confidence,
                    judge_result=judge_result,
                )
            )
            active_previous.raw_text = text
            active_previous.updated_at = now
            active_previous.relationship_to_active_task = relationship
            active_previous.status = "active"
            if effect == "create_candidate":
                active_previous.last_relevance_check = {
                    "verdict": "possible_new_task",
                    "reason": "structured relationship judge marked this as a possible separate task; active task preserved",
                    "previous_raw_text": active_previous.task_contract.raw_primary_text or active_previous.raw_text,
                    "new_raw_text": text,
                }
            self._state = active_previous
        else:
            contract = TaskContract(raw_primary_text=text, raw_supplements=[], derived_metadata={})
            contract.refresh_metadata()
            relevance = None
            if active_previous and effect == "supersede":
                active_previous.status = "superseded"
                relevance = {
                    "verdict": "replacement",
                    "reason": "structured relationship judge marked this as replacing the previous task",
                    "previous_raw_text": active_previous.task_contract.raw_primary_text or active_previous.raw_text,
                    "new_raw_text": text,
                }
            elif active_previous and effect == "pause_and_start":
                active_previous.status = "paused"
                relevance = {
                    "verdict": "new_task",
                    "reason": "structured relationship judge marked this as a separate new task; previous task is paused",
                    "previous_raw_text": active_previous.task_contract.raw_primary_text or active_previous.raw_text,
                    "new_raw_text": text,
                }
            state = TaskIntentState(
                id=_new_id("task"),
                kind="direct_user_message",
                raw_text=text,
                created_at=now,
                updated_at=now,
                status="active",
                relationship_to_active_task=relationship,
                task_contract=contract,
                completion_state={"last_verdict": "unknown"},
                machine_preserved_messages=[],
                raw_messages=[
                    RawTaskMessage.create(
                        text,
                        source="user",
                        relationship_to_active_task=relationship,
                        source_kind=source_kind,
                        message_id=message_id,
                        state_effect=effect,
                        relationship_confidence=decision.confidence,
                        judge_result=judge_result,
                    )
                ],
                last_relevance_check=relevance,
            )
            self._state = state

        save_task_intent(self.session_id, self._state)
        return self._state

    def add_machine_preserved_message(
        self,
        raw_text: str,
        *,
        origin: str = "other",
        status: str = "queued",
    ) -> PreservedMachineMessage:
        if self._state is None:
            self.record_direct_message("")
        assert self._state is not None
        item = PreservedMachineMessage.create(raw_text, origin=origin, status=status)
        self._state.machine_preserved_messages.append(item)
        self._state.raw_messages.append(
            RawTaskMessage.create(
                raw_text,
                source="machine",
                relationship_to_active_task=origin,
                source_kind="machine",
                state_effect="no_change",
            )
        )
        self._state.updated_at = _now()
        save_task_intent(self.session_id, self._state)
        return item

    def evaluate_after_response(self, response: str, *, claimed_verdict: Optional[str] = None) -> Dict[str, Any]:
        if self._state is None:
            return {"verdict": "inactive", "guard_vetoed_done": False, "reason": "no active direct-message task"}
        text = str(response or "")
        contract = self._state.task_contract
        machine_texts = [m.raw_text for m in self._state.machine_preserved_messages]
        response_claims_done = bool(re.search(r"\b(done|completed|complete|finished)\b", text, re.I))
        proposed_done = (claimed_verdict == "done") or response_claims_done

        guard_vetoed = should_veto_done_for_multiplicity(
            raw_task_text="\n".join([contract.raw_primary_text, *contract.raw_supplements]),
            response=text,
            raw_machine_texts=machine_texts,
        ) and proposed_done
        if guard_vetoed:
            verdict = "continue"
            reason = "done vetoed: preserved raw task implies multiple/additional outputs but response only completed one slice"
            self._state.status = "active"
        elif proposed_done:
            verdict = "done"
            reason = "response claims completion and no local direct-message guard veto applied"
            self._state.status = "completed"
        else:
            verdict = "continue"
            reason = "response did not clearly complete the active direct-message task"
            self._state.status = "active"

        self._state.completion_state = {
            "last_verdict": verdict,
            "last_reason": reason,
            "guard_vetoed_done": guard_vetoed,
        }
        self._state.updated_at = _now()
        save_task_intent(self.session_id, self._state)
        return {"verdict": verdict, "guard_vetoed_done": guard_vetoed, "reason": reason}

    def format_preserved_state_notice(self) -> str:
        if self._state is None:
            return ""
        parts: List[str] = []
        if self._state.last_relevance_check:
            verdict = self._state.last_relevance_check.get("verdict")
            previous = self._state.last_relevance_check.get("previous_raw_text") or ""
            if verdict in {"replacement", "unclear", "new_task"} and previous:
                if verdict == "replacement":
                    label = "may no longer be relevant"
                elif verdict == "new_task":
                    label = "is paused because your latest direct message looks like a new task"
                else:
                    label = "may be ambiguous"
                parts.append(
                    "Previous direct-message task " + label + ":\n\n---\n" + previous + "\n---"
                )
        active_machine = [m for m in self._state.machine_preserved_messages if m.status not in {"discarded", "completed"}]
        for item in active_machine:
            parts.append(
                "Machine-preserved ongoing message (raw, "
                f"origin={item.origin}, status={item.status}):\n\n---\n{item.raw_text}\n---"
            )
        return "\n\n".join(parts)


__all__ = [
    "TaskContract",
    "PreservedMachineMessage",
    "TaskRelationshipDecision",
    "RawTaskMessage",
    "TaskIntentState",
    "TaskIntentManager",
    "clamp_raw_text",
    "derive_contract_metadata",
    "validate_judge_payload_no_rewrite",
    "should_veto_done_for_multiplicity",
    "load_task_intent",
    "save_task_intent",
]
