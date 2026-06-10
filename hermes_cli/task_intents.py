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

_SCOPE_REDUCTION_PREFIXES = (
    "actually just ",
    "actually only ",
    "just ",
    "only ",
    "instead ",
    "instead, ",
    "switch to ",
    "stop that ",
    "stop this ",
    "stop the previous task",
    "cancel the previous task",
    "ignore the previous task",
    "ignore the earlier task",
    "forget the previous task",
    "forget the earlier task",
)

_SUPPLEMENT_PREFIXES = (
    "also ",
    "and ",
    "but ",
    "use ",
    "keep ",
    "make sure ",
    "no need ",
    "don't need ",
    "do not need ",
    "you can ",
    "for the first pass",
)

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


def _is_scope_reduction(text: str) -> bool:
    n = _norm(text)
    return any(n.startswith(prefix) for prefix in _SCOPE_REDUCTION_PREFIXES)


def _looks_like_supplement(text: str) -> bool:
    n = _norm(text)
    return any(n.startswith(prefix) for prefix in _SUPPLEMENT_PREFIXES)


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
    for key, value in payload.items():
        if str(key).lower() in _FORBIDDEN_JUDGE_REWRITE_KEYS:
            rejected.append(str(key))
            continue
        cleaned[key] = value

    raw_haystack = "\n".join(str(t or "") for t in (raw_texts or []))
    quotes = cleaned.get("evidence_quotes")
    if isinstance(quotes, list):
        cleaned["evidence_quotes"] = [
            quote
            for quote in quotes
            if isinstance(quote, str) and quote and quote in raw_haystack
        ]

    if rejected:
        cleaned["_rejected_rewrite_keys"] = rejected
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
        "explicit_scope_reduction": any(_is_scope_reduction(t) for t in texts if t),
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
class RawTaskMessage:
    raw_text: str
    source: str = "user"
    relationship_to_active_task: str = "new_task"
    created_at: float = 0.0
    id: str = ""

    @classmethod
    def create(
        cls,
        raw_text: str,
        *,
        source: str = "user",
        relationship_to_active_task: str = "new_task",
    ) -> "RawTaskMessage":
        return cls(
            raw_text=str(raw_text or ""),
            source=source,
            relationship_to_active_task=relationship_to_active_task,
            created_at=_now(),
            id=_new_id("raw"),
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RawTaskMessage":
        return cls(
            raw_text=str((data or {}).get("raw_text") or ""),
            source=str((data or {}).get("source") or "user"),
            relationship_to_active_task=str((data or {}).get("relationship_to_active_task") or "new_task"),
            created_at=float((data or {}).get("created_at") or 0.0),
            id=str((data or {}).get("id") or _new_id("raw")),
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

    def classify_relationship(self, text: str) -> str:
        if self._state is None or self._state.status in {"completed", "discarded", "superseded"}:
            return "new_task"
        if _is_scope_reduction(text):
            return "replacement"
        if _looks_like_supplement(text):
            return "supplement"
        # While an active raw contract exists, default to preserving/merging the
        # latest direct message. A future LLM judge may label high-confidence
        # unrelated tasks, but that judge still must not rewrite user wording.
        n = _norm(text)
        if n in {"try the other one", "do that", "continue", "go on"}:
            return "unclear"
        return "supplement"

    def record_direct_message(self, raw_text: str) -> TaskIntentState:
        text = str(raw_text or "")
        relationship = self.classify_relationship(text)
        previous = self._state
        now = _now()

        if previous and relationship in {"supplement", "unclear"}:
            contract = previous.task_contract
            if text and text not in contract.raw_supplements:
                contract.raw_supplements.append(text)
            contract.refresh_metadata()
            previous.raw_messages.append(
                RawTaskMessage.create(text, source="user", relationship_to_active_task=relationship)
            )
            previous.raw_text = text
            previous.updated_at = now
            previous.relationship_to_active_task = relationship
            previous.status = "active"
            self._state = previous
        else:
            contract = TaskContract(raw_primary_text=text, raw_supplements=[], derived_metadata={})
            contract.refresh_metadata()
            relevance = None
            if previous and relationship == "replacement":
                previous.status = "superseded"
                relevance = {
                    "verdict": "replacement",
                    "reason": "latest direct message explicitly switches/replaces the previous task",
                    "previous_raw_text": previous.task_contract.raw_primary_text or previous.raw_text,
                    "new_raw_text": text,
                }
            elif previous and relationship == "unclear":
                relevance = {
                    "verdict": "unclear",
                    "reason": "latest direct message may or may not relate to previous task",
                    "previous_raw_text": previous.task_contract.raw_primary_text or previous.raw_text,
                    "new_raw_text": text,
                }
            elif previous and relationship == "new_task":
                previous.status = "paused"
                relevance = {
                    "verdict": "new_task",
                    "reason": "latest direct message appears to start a new task; previous task is paused",
                    "previous_raw_text": previous.task_contract.raw_primary_text or previous.raw_text,
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
                    RawTaskMessage.create(text, source="user", relationship_to_active_task=relationship)
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
            RawTaskMessage.create(raw_text, source="machine", relationship_to_active_task=origin)
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
