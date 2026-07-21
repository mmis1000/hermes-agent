"""Bounded relationship micro-judge for gateway direct-message intent.

This is a tiny auxiliary classifier, never an :class:`AIAgent` turn: it receives
no tools, transcript, persona, or generated task wording.  Callers supply the
current auxiliary ``call_llm`` wrapper and enforce an outer async deadline.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from hermes_cli.task_intents import (
    TaskIntentState,
    TaskRelationshipDecision,
    clamp_raw_text,
)


SCHEMA_VERSION = "task-intent-relationship-v2"
POLICY_VERSION = "raw-source-authority-v2"
_AUTHORITATIVE_JUDGE_SOURCES = {"direct_user", "direct_external_user"}


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return bool(value)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
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
    max_primary_chars: int = 700
    max_supplement_chars: int = 240
    max_message_chars: int = 700
    max_recent_supplements: int = 3
    max_prompt_chars: int = 4200
    cache_size: int = 512
    schema_version: str = SCHEMA_VERSION
    policy_version: str = POLICY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", _coerce_bool(self.enabled, True))
        object.__setattr__(
            self,
            "timeout_seconds",
            _bounded_float(self.timeout_seconds, 1.5, 0.05, 10.0),
        )
        object.__setattr__(
            self,
            "max_output_tokens",
            _bounded_int(self.max_output_tokens, 160, 32, 256),
        )
        object.__setattr__(
            self, "max_primary_chars", _bounded_int(self.max_primary_chars, 700, 16, 2000)
        )
        object.__setattr__(
            self,
            "max_supplement_chars",
            _bounded_int(self.max_supplement_chars, 240, 16, 1000),
        )
        object.__setattr__(
            self, "max_message_chars", _bounded_int(self.max_message_chars, 700, 16, 2000)
        )
        object.__setattr__(
            self,
            "max_recent_supplements",
            _bounded_int(self.max_recent_supplements, 3, 0, 8),
        )
        object.__setattr__(
            self, "max_prompt_chars", _bounded_int(self.max_prompt_chars, 4200, 1200, 8000)
        )
        object.__setattr__(
            self, "cache_size", _bounded_int(self.cache_size, 512, 0, 4096)
        )
        object.__setattr__(self, "schema_version", str(self.schema_version or SCHEMA_VERSION))
        object.__setattr__(self, "policy_version", str(self.policy_version or POLICY_VERSION))

    @classmethod
    def from_mapping(cls, data: Optional[Any]) -> "TaskIntentMicroJudgeConfig":
        # Runtime configuration is opt-in. Direct construction remains useful
        # for focused tests and explicitly-created callers.
        if data is None:
            return cls(enabled=False)
        if isinstance(data, bool):
            return cls(enabled=data)
        if not isinstance(data, Mapping):
            return cls(enabled=False)
        return cls(
            enabled=_coerce_bool(data.get("enabled", True), True),
            timeout_seconds=data.get("timeout_seconds", data.get("timeout", 1.5)),
            max_output_tokens=data.get("max_output_tokens", 160),
            max_primary_chars=data.get("max_primary_chars", 700),
            max_supplement_chars=data.get("max_supplement_chars", 240),
            max_message_chars=data.get("max_message_chars", 700),
            max_recent_supplements=data.get("max_recent_supplements", 3),
            max_prompt_chars=data.get("max_prompt_chars", 4200),
            cache_size=data.get("cache_size", 512),
            schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
            policy_version=str(data.get("policy_version") or POLICY_VERSION),
        )

    def signature(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))


def _hash_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _active_task_version(state: TaskIntentState) -> str:
    return f"{state.id}:{state.updated_at:.6f}:{len(state.raw_messages)}"


def build_relationship_judge_payload(
    *,
    state: TaskIntentState,
    current_message: str,
    message_id: str = "",
    source_kind: str = "direct_user",
    config: Optional[TaskIntentMicroJudgeConfig] = None,
) -> Dict[str, Any]:
    config = config or TaskIntentMicroJudgeConfig()
    supplements = list(state.task_contract.raw_supplements)
    if config.max_recent_supplements:
        supplements = supplements[-config.max_recent_supplements :]
    else:
        supplements = []
    return {
        "schema_version": config.schema_version,
        "policy_version": config.policy_version,
        "source": {"kind": source_kind, "message_id": str(message_id or "")},
        "active_task": {
            "id": state.id,
            "version": _active_task_version(state),
            "primary_text": clamp_raw_text(
                state.task_contract.raw_primary_text, config.max_primary_chars
            ),
            "recent_supplements": [
                clamp_raw_text(item, config.max_supplement_chars) for item in supplements
            ],
        },
        "current_message": {
            "id": str(message_id or ""),
            "text": clamp_raw_text(current_message, config.max_message_chars),
        },
    }


def build_relationship_judge_messages(
    payload: Dict[str, Any], *, config: Optional[TaskIntentMicroJudgeConfig] = None
) -> List[Dict[str, str]]:
    config = config or TaskIntentMicroJudgeConfig()
    system = (
        "Classify the semantic relationship of one authoritative direct-user message "
        "to one active task. Return exactly one JSON object and no markdown. Never "
        "rewrite, summarize, normalize, translate, or clean either text. No tools. "
        "Allowed relationship/state_effect pairs: new_task/start_new, "
        "same_task/no_change, related_question/no_change, "
        "supplement/append_contract, replacement/supersede, "
        "cancellation/cancel, unclear/no_change. Output keys only: relationship, "
        "state_effect, confidence (0..1), reason_codes (string array), "
        "evidence_quotes (exact substrings of current_message.text). Destructive "
        "effects require high confidence and exact current-message evidence. When "
        "ambiguous choose unclear/no_change."
    )
    user = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(user) > config.max_prompt_chars:
        raise ValueError("task-intent judge payload exceeds configured bound")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_strict_json_object(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    try:
        return str(response.choices[0].message.content or "")
    except Exception:
        return ""


def _clone_decision(
    decision: TaskRelationshipDecision, *, cache_status: Optional[str] = None
) -> TaskRelationshipDecision:
    raw_payload = dict(decision.raw_payload)
    if cache_status is not None:
        raw_payload["cache"] = cache_status
    return TaskRelationshipDecision(
        relationship=decision.relationship,
        state_effect=decision.state_effect,
        confidence=decision.confidence,
        reason_codes=list(decision.reason_codes),
        evidence_quotes=list(decision.evidence_quotes),
        raw_payload=raw_payload,
        fallback_reason=decision.fallback_reason,
    )


class TaskIntentMicroJudge:
    def __init__(
        self,
        *,
        llm_call: Callable[..., Any],
        config: Optional[TaskIntentMicroJudgeConfig] = None,
        cache: Optional[OrderedDict[str, TaskRelationshipDecision]] = None,
        cache_lock: Any = None,
    ) -> None:
        self.config = config or TaskIntentMicroJudgeConfig()
        self._llm_call = llm_call
        self._cache = cache if cache is not None else OrderedDict()
        self._cache_lock = cache_lock

    def cache_key(
        self,
        *,
        state: TaskIntentState,
        current_message: str,
        message_id: str = "",
        source_kind: str = "direct_user",
    ) -> str:
        raw = {
            "schema": self.config.schema_version,
            "policy": self.config.policy_version,
            "config": self.config.signature(),
            "source_kind": source_kind,
            "message_id": str(message_id or ""),
            "task_id": state.id,
            "task_version": _active_task_version(state),
            "raw_current_sha256": _hash_text(current_message),
            "raw_primary_sha256": _hash_text(state.task_contract.raw_primary_text),
            "raw_supplements_sha256": hashlib.sha256(
                json.dumps(
                    state.task_contract.raw_supplements,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }
        return hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def get_cached_decision(self, key: str) -> Optional[TaskRelationshipDecision]:
        lock = self._cache_lock
        if lock is not None:
            with lock:
                return self._get_cached_unlocked(key)
        return self._get_cached_unlocked(key)

    def _get_cached_unlocked(self, key: str) -> Optional[TaskRelationshipDecision]:
        decision = self._cache.get(key)
        if decision is None:
            return None
        self._cache.move_to_end(key)
        return _clone_decision(decision, cache_status="hit")

    def store_cached_decision(self, key: str, decision: TaskRelationshipDecision) -> None:
        if self.config.cache_size <= 0:
            return
        lock = self._cache_lock
        if lock is not None:
            with lock:
                self._store_cached_unlocked(key, decision)
            return
        self._store_cached_unlocked(key, decision)

    def _store_cached_unlocked(self, key: str, decision: TaskRelationshipDecision) -> None:
        self._cache[key] = _clone_decision(decision, cache_status="stored")
        self._cache.move_to_end(key)
        while len(self._cache) > self.config.cache_size:
            self._cache.popitem(last=False)

    def judge(
        self,
        *,
        state: Optional[TaskIntentState],
        current_message: str,
        message_id: str = "",
        source_kind: str = "direct_user",
        use_cache: bool = True,
    ) -> Optional[TaskRelationshipDecision]:
        if not self.config.enabled:
            return None
        if source_kind not in _AUTHORITATIVE_JUDGE_SOURCES:
            return None
        if state is None or state.status != "active" or not str(current_message or "").strip():
            return None

        key = self.cache_key(
            state=state,
            current_message=current_message,
            message_id=message_id,
            source_kind=source_kind,
        )
        if use_cache:
            cached = self.get_cached_decision(key)
            if cached is not None:
                return cached

        payload = build_relationship_judge_payload(
            state=state,
            current_message=current_message,
            message_id=message_id,
            source_kind=source_kind,
            config=self.config,
        )
        messages = build_relationship_judge_messages(payload, config=self.config)
        started = time.monotonic()
        try:
            response = self._llm_call(
                messages=messages,
                timeout=self.config.timeout_seconds,
                max_tokens=self.config.max_output_tokens,
            )
            parsed = _parse_strict_json_object(_response_text(response))
            decision = TaskRelationshipDecision.from_payload(
                parsed, raw_text=str(current_message or "")
            )
            if not parsed:
                decision.reason_codes.append("invalid_judge_json")
                decision.fallback_reason = "judge returned invalid or empty JSON"
            decision.raw_payload["cache"] = "miss"
            decision.raw_payload["cache_key"] = key
            decision.raw_payload["latency_ms"] = int(
                (time.monotonic() - started) * 1000
            )
        except Exception as exc:
            return TaskRelationshipDecision(
                relationship="unclear",
                state_effect="no_change",
                confidence=0.0,
                reason_codes=["judge_error"],
                raw_payload={
                    "cache": "uncached_error",
                    "cache_key": key,
                    "error_type": type(exc).__name__,
                },
                fallback_reason="relationship judge failed",
            )

        if use_cache and parsed:
            self.store_cached_decision(key, decision)
        return _clone_decision(decision, cache_status="miss")


__all__ = [
    "TaskIntentMicroJudge",
    "TaskIntentMicroJudgeConfig",
    "build_relationship_judge_messages",
    "build_relationship_judge_payload",
]
