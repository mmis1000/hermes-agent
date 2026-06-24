"""Bounded hot-path relationship judge for direct-message task intent.

This module owns the small, cacheable classifier request used by the gateway. It
is deliberately not an agent turn: no tools, no transcript replay, no persona,
and no rewritten task text. Raw task wording remains canonical in
``task_intents``; this judge only supplies structured relationship metadata.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, Callable, Dict, List, Optional

from hermes_cli.task_intents import (
    RELATIONSHIP_LABELS,
    STATE_EFFECTS,
    _ALLOWED_RELATIONSHIP_EFFECTS,
    TaskIntentState,
    TaskRelationshipDecision,
    clamp_raw_text,
)

JUDGE_SCHEMA_VERSION = "task-intent-relationship-v1"
DEFAULT_POLICY_VERSION = "no-rewrite-source-aware-v1"

_ALLOWED_RELATIONSHIPS = [
    "same_task",
    "related_question",
    "supplement",
    "possible_new_task",
    "new_task",
    "replacement",
    "unclear",
]
_ALLOWED_EFFECTS = [
    "no_change",
    "related_only",
    "append_contract",
    "create_candidate",
    "pause_and_start",
    "supersede",
]
_RELATIONSHIP_EFFECTS = {
    relationship: sorted(effects)
    for relationship, effects in _ALLOWED_RELATIONSHIP_EFFECTS.items()
}
assert set(_ALLOWED_RELATIONSHIPS) == set(RELATIONSHIP_LABELS)
assert set(_ALLOWED_EFFECTS) == set(STATE_EFFECTS)
_FORBIDDEN_REWRITE_FIELDS = [
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
]


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return bool(value)


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


@dataclass(frozen=True)
class TaskIntentMicroJudgeConfig:
    enabled: bool = True
    timeout_seconds: float = 1.5
    max_output_tokens: int = 160
    max_primary_chars: int = 800
    max_supplement_chars: int = 320
    max_message_chars: int = 800
    max_recent_supplements: int = 3
    cache_size: int = 512
    policy_version: str = DEFAULT_POLICY_VERSION
    prompt_version: str = JUDGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", _coerce_bool(self.enabled, True))
        object.__setattr__(self, "timeout_seconds", _clamp_float(self.timeout_seconds, 1.5, 0.05, 10.0))
        object.__setattr__(self, "max_output_tokens", _clamp_int(self.max_output_tokens, 160, 32, 1000))
        object.__setattr__(self, "max_primary_chars", _clamp_int(self.max_primary_chars, 800, 1, 8000))
        object.__setattr__(self, "max_supplement_chars", _clamp_int(self.max_supplement_chars, 320, 1, 4000))
        object.__setattr__(self, "max_message_chars", _clamp_int(self.max_message_chars, 800, 1, 8000))
        object.__setattr__(self, "max_recent_supplements", _clamp_int(self.max_recent_supplements, 3, 0, 20))
        object.__setattr__(self, "cache_size", _clamp_int(self.cache_size, 512, 0, 10000))
        object.__setattr__(self, "policy_version", str(self.policy_version or DEFAULT_POLICY_VERSION))
        object.__setattr__(self, "prompt_version", str(self.prompt_version or JUDGE_SCHEMA_VERSION))

    @classmethod
    def from_mapping(cls, data: Optional[Any]) -> "TaskIntentMicroJudgeConfig":
        if isinstance(data, bool):
            return cls(enabled=data)
        if data is not None and not isinstance(data, Mapping):
            return cls(enabled=False)
        cfg = data or {}
        return cls(
            enabled=_coerce_bool(cfg.get("enabled", True), True),
            timeout_seconds=_clamp_float(cfg.get("timeout_seconds", cfg.get("timeout", 1.5)), 1.5, 0.05, 10.0),
            max_output_tokens=_clamp_int(cfg.get("max_output_tokens", 160), 160, 32, 1000),
            max_primary_chars=_clamp_int(cfg.get("max_primary_chars", 800), 800, 80, 8000),
            max_supplement_chars=_clamp_int(cfg.get("max_supplement_chars", 320), 320, 40, 4000),
            max_message_chars=_clamp_int(cfg.get("max_message_chars", 800), 800, 80, 8000),
            max_recent_supplements=_clamp_int(cfg.get("max_recent_supplements", 3), 3, 0, 20),
            cache_size=_clamp_int(cfg.get("cache_size", 512), 512, 0, 10000),
            policy_version=str(cfg.get("policy_version") or DEFAULT_POLICY_VERSION),
            prompt_version=str(cfg.get("prompt_version") or JUDGE_SCHEMA_VERSION),
        )

    def signature(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))


def _active_task_version(state: TaskIntentState) -> str:
    return f"{state.id}:{state.updated_at:.6f}:{len(state.raw_messages)}"


def _sha256_json(data: Dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _extract_json_object(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def build_relationship_judge_payload(
    *,
    state: TaskIntentState,
    current_message: str,
    message_id: str = "",
    source_kind: str = "direct_user",
    config: Optional[TaskIntentMicroJudgeConfig] = None,
) -> Dict[str, Any]:
    cfg = config or TaskIntentMicroJudgeConfig()
    all_supplements = list(state.task_contract.raw_supplements or [])
    supplements = all_supplements[-cfg.max_recent_supplements :] if cfg.max_recent_supplements > 0 else []
    return {
        "schema_version": cfg.prompt_version,
        "policy_version": cfg.policy_version,
        "source": {
            "kind": source_kind,
            "message_id": str(message_id or ""),
        },
        "active_task": {
            "id": state.id,
            "version": _active_task_version(state),
            "primary_text": clamp_raw_text(state.task_contract.raw_primary_text, cfg.max_primary_chars),
            "recent_supplements": [
                clamp_raw_text(item, cfg.max_supplement_chars)
                for item in supplements
                if str(item or "")
            ],
        },
        "current_message": {
            "id": str(message_id or ""),
            "text": clamp_raw_text(current_message, cfg.max_message_chars),
        },
        "allowed_relationships": list(_ALLOWED_RELATIONSHIPS),
        "allowed_state_effects": list(_ALLOWED_EFFECTS),
        "forbidden_rewrite_fields": list(_FORBIDDEN_REWRITE_FIELDS),
    }


def build_relationship_judge_messages(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    system = (
        "You are a tiny task-intent relationship classifier for Hermes Agent. "
        "Return one JSON object only. Do not use tools. Do not include markdown. "
        "Do not rewrite, summarize, translate, normalize, or clean the user's task. "
        "Classify only how the current direct user message relates to the active task. "
        "Use evidence_quotes only when the quote is an exact substring of the raw input. "
        "High-impact task changes require high confidence. If uncertain, choose "
        "relationship='unclear' and state_effect='no_change'."
    )
    user = {
        "instructions": {
            "output_schema": {
                "relationship": "same_task | related_question | supplement | possible_new_task | new_task | replacement | unclear",
                "state_effect": "no_change | related_only | append_contract | create_candidate | pause_and_start | supersede",
                "confidence": "number 0.0..1.0",
                "reason_codes": ["short_semantic_reason_codes"],
                "evidence_quotes": ["exact substrings only"],
            },
            "allowed_relationship_effects": _RELATIONSHIP_EFFECTS,
            "effects": {
                "same_task": "the message continues the active task without adding a durable new requirement",
                "related_question": "the message asks about/clarifies the active task but adds no deliverable",
                "supplement": "the message adds a requirement/constraint to the active task",
                "possible_new_task": "it may be separate; preserve active task and create a candidate only",
                "new_task": "it is clearly a separate task; pause active task and start this one",
                "replacement": "it clearly supersedes the active task",
                "unclear": "insufficient confidence; preserve active task without mutation",
            },
            "forbidden": payload.get("forbidden_rewrite_fields", []),
        },
        "input": payload,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False, separators=(",", ":"))},
    ]


class TaskIntentMicroJudge:
    """Small cached relationship judge wrapper.

    ``llm_call`` receives ``messages``, ``timeout`` and ``max_tokens`` keyword
    arguments and returns either text or a response with ``choices[0].message``.
    """

    def __init__(
        self,
        *,
        config: Optional[TaskIntentMicroJudgeConfig] = None,
        llm_call: Callable[..., Any],
        cache: Optional[OrderedDict[str, TaskRelationshipDecision]] = None,
        cache_lock: Optional[Any] = None,
    ) -> None:
        self.config = config or TaskIntentMicroJudgeConfig()
        self._llm_call = llm_call
        self._cache: OrderedDict[str, TaskRelationshipDecision] = cache if cache is not None else OrderedDict()
        self._cache_lock = cache_lock

    def cache_key(
        self,
        *,
        state: TaskIntentState,
        current_message: str,
        message_id: str = "",
        source_kind: str = "direct_user",
    ) -> str:
        payload = build_relationship_judge_payload(
            state=state,
            current_message=current_message,
            message_id=message_id,
            source_kind=source_kind,
            config=self.config,
        )
        return _sha256_json({
            "kind": "task_intent_relationship",
            "payload": payload,
            "config": self.config.signature(),
            "raw_current_message_sha256": hashlib.sha256(str(current_message or "").encode("utf-8")).hexdigest(),
            "raw_primary_sha256": hashlib.sha256(str(state.task_contract.raw_primary_text or "").encode("utf-8")).hexdigest(),
            "raw_supplements_sha256": hashlib.sha256(
                json.dumps(list(state.task_contract.raw_supplements or []), ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
        })

    def get_cached_decision(self, key: str) -> Optional[TaskRelationshipDecision]:
        return self._get_cached(key)

    def store_cached_decision(self, key: str, decision: TaskRelationshipDecision) -> None:
        self._store_cache(key, decision)

    def judge(
        self,
        *,
        state: TaskIntentState,
        current_message: str,
        message_id: str = "",
        source_kind: str = "direct_user",
        use_cache: bool = True,
    ) -> Optional[TaskRelationshipDecision]:
        if not self.config.enabled:
            return None
        if state is None or state.status != "active":
            return None
        raw = str(current_message or "")
        if not raw.strip():
            return None

        key = self.cache_key(
            state=state,
            current_message=raw,
            message_id=message_id,
            source_kind=source_kind,
        )
        cached = self._get_cached(key) if use_cache else None
        if cached is not None:
            return cached

        payload = build_relationship_judge_payload(
            state=state,
            current_message=raw,
            message_id=message_id,
            source_kind=source_kind,
            config=self.config,
        )
        messages = build_relationship_judge_messages(payload)
        started = time.monotonic()
        try:
            response = self._llm_call(
                messages=messages,
                timeout=self.config.timeout_seconds,
                max_tokens=self.config.max_output_tokens,
            )
            content = _response_text(response)
            parsed = _extract_json_object(content)
            decision = TaskRelationshipDecision.from_payload(
                parsed,
                raw_texts=[
                    state.task_contract.raw_primary_text,
                    *state.task_contract.raw_supplements,
                    raw,
                ],
            )
            if not parsed:
                decision.fallback_reason = "judge returned invalid or empty JSON"
                decision.reason_codes.append("invalid_judge_json")
            decision.raw_payload.setdefault("cache", "miss")
            decision.raw_payload.setdefault("latency_ms", int((time.monotonic() - started) * 1000))
            decision.raw_payload.setdefault("cache_key", key)
        except Exception as exc:
            return TaskRelationshipDecision(
                relationship="unclear",
                state_effect="no_change",
                confidence=0.0,
                reason_codes=["judge_error"],
                raw_payload={"cache": "uncached_error", "cache_key": key, "error_type": type(exc).__name__},
                fallback_reason="relationship judge failed",
            )

        if use_cache:
            self._store_cache(key, decision)
        return _clone_decision(decision, cache_status="miss")

    def _get_cached(self, key: str) -> Optional[TaskRelationshipDecision]:
        lock = self._cache_lock
        if lock is not None:
            with lock:
                cached = self._cache.get(key)
                if cached is None:
                    return None
                self._cache.move_to_end(key)
                return _clone_decision(cached, cache_status="hit")
        cached = self._cache.get(key)
        if cached is None:
            return None
        self._cache.move_to_end(key)
        return _clone_decision(cached, cache_status="hit")

    def _store_cache(self, key: str, decision: TaskRelationshipDecision) -> None:
        if self.config.cache_size <= 0:
            return
        lock = self._cache_lock
        if lock is not None:
            with lock:
                self._store_cache_unlocked(key, decision)
            return
        self._store_cache_unlocked(key, decision)

    def _store_cache_unlocked(self, key: str, decision: TaskRelationshipDecision) -> None:
        self._cache[key] = _clone_decision(decision, cache_status="stored")
        self._cache.move_to_end(key)
        while len(self._cache) > self.config.cache_size:
            self._cache.popitem(last=False)


def _clone_decision(decision: TaskRelationshipDecision, *, cache_status: Optional[str] = None) -> TaskRelationshipDecision:
    raw_payload = dict(decision.raw_payload or {})
    if cache_status is not None:
        raw_payload["cache"] = cache_status
    return TaskRelationshipDecision(
        relationship=decision.relationship,
        state_effect=decision.state_effect,
        confidence=decision.confidence,
        reason_codes=list(decision.reason_codes or []),
        evidence_quotes=list(decision.evidence_quotes or []),
        raw_payload=raw_payload,
        fallback_reason=decision.fallback_reason,
    )


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    try:
        return str(response.choices[0].message.content or "")
    except Exception:
        return str(response or "")


__all__ = [
    "TaskIntentMicroJudge",
    "TaskIntentMicroJudgeConfig",
    "build_relationship_judge_messages",
    "build_relationship_judge_payload",
]
