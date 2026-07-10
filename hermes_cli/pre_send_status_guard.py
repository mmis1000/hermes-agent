"""Optional pre-send task-status and answer-coverage sanity guard.

The guard is deliberately not a release/delivery classifier. It builds a
neutral synopsis of recent operations -- tool calls, terminal commands, tool
results, skill reads, and notable user/system events -- and asks a tiny judge
whether the candidate visible response makes unsupported task-status claims or
uses uncertainty as a reason to omit requested material without an explicit
caveat.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

from hermes_cli.task_intents import TaskIntentState, clamp_raw_text

STATUS_GUARD_SCHEMA_VERSION = "pre-send-status-guard-v1"
DEFAULT_POLICY_VERSION = "neutral-operation-synopsis-v1"
DEFAULT_STATUS_GUARD_JUDGE_PROMPT = (
    "You are a tiny optional pre-send status-sanity judge for Hermes Agent. "
    "Return one JSON object only; no markdown. You are not a task planner and do not use tools. "
    "Given an active task, a candidate visible assistant response, and a neutral recent-operation synopsis, "
    "decide whether the candidate response makes status claims that are unsupported or contradicted by the shown operations. "
    "Do not classify commands in advance; reason from the exact tool names, command excerpts, skill reads, user events, and results shown. "
    "Reject when the draft claims completion, verification, review approval, upload/send/delivery, deployment, commit/push, or other task status that the operations do not support. "
    "Also reject drafts that use uncertainty or non-100%-confidence as a reason to ignore, omit, skip, exclude, drop, or silently leave out user-requested material instead of answering with explicit uncertainty/caveats. "
    "Allow honest progress/blocker/partial-status messages and answers that state uncertainty while still addressing the requested material. If uncertain about whether a draft violates this policy, allow."
)

_DECISIONS = {"allow", "reject_and_steer"}


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


def _safe_json_loads(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    try:
        return str(response.choices[0].message.content or "")
    except Exception:
        return str(response or "")


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


def _redact(text: str) -> str:
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(str(text or ""), force=True)
    except Exception:
        return str(text or "")


def _clamp_redacted(text: Any, limit: int) -> str:
    return clamp_raw_text(_redact(str(text or "")), limit)


@dataclass(frozen=True)
class PreSendStatusGuardConfig:
    enabled: bool = False
    timeout_seconds: float = 2.0
    max_output_tokens: int = 220
    max_operations: int = 30
    terminal_command_chars: int = 320
    tool_arguments_chars: int = 400
    tool_result_chars: int = 600
    user_event_chars: int = 600
    max_candidate_chars: int = 1200
    max_primary_chars: int = 1200
    max_supplement_chars: int = 500
    max_recent_supplements: int = 5
    cache_size: int = 256
    policy_version: str = DEFAULT_POLICY_VERSION
    prompt_version: str = STATUS_GUARD_SCHEMA_VERSION
    judge_prompt: str = DEFAULT_STATUS_GUARD_JUDGE_PROMPT

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", _coerce_bool(self.enabled, False))
        object.__setattr__(self, "timeout_seconds", _clamp_float(self.timeout_seconds, 2.0, 0.05, 20.0))
        object.__setattr__(self, "max_output_tokens", _clamp_int(self.max_output_tokens, 220, 64, 1200))
        object.__setattr__(self, "max_operations", _clamp_int(self.max_operations, 30, 1, 120))
        object.__setattr__(self, "terminal_command_chars", _clamp_int(self.terminal_command_chars, 320, 40, 4000))
        object.__setattr__(self, "tool_arguments_chars", _clamp_int(self.tool_arguments_chars, 400, 40, 4000))
        object.__setattr__(self, "tool_result_chars", _clamp_int(self.tool_result_chars, 600, 40, 8000))
        object.__setattr__(self, "user_event_chars", _clamp_int(self.user_event_chars, 600, 40, 8000))
        object.__setattr__(self, "max_candidate_chars", _clamp_int(self.max_candidate_chars, 1200, 80, 8000))
        object.__setattr__(self, "max_primary_chars", _clamp_int(self.max_primary_chars, 1200, 80, 8000))
        object.__setattr__(self, "max_supplement_chars", _clamp_int(self.max_supplement_chars, 500, 40, 4000))
        object.__setattr__(self, "max_recent_supplements", _clamp_int(self.max_recent_supplements, 5, 0, 20))
        object.__setattr__(self, "cache_size", _clamp_int(self.cache_size, 256, 0, 10000))
        object.__setattr__(self, "policy_version", str(self.policy_version or DEFAULT_POLICY_VERSION))
        object.__setattr__(self, "prompt_version", str(self.prompt_version or STATUS_GUARD_SCHEMA_VERSION))
        object.__setattr__(self, "judge_prompt", str(self.judge_prompt or DEFAULT_STATUS_GUARD_JUDGE_PROMPT))

    @classmethod
    def from_mapping(cls, data: Optional[Any]) -> "PreSendStatusGuardConfig":
        if isinstance(data, bool):
            return cls(enabled=data)
        if data is not None and not isinstance(data, Mapping):
            return cls(enabled=False)
        cfg = data or {}
        return cls(
            enabled=_coerce_bool(cfg.get("enabled", False), False),
            timeout_seconds=_clamp_float(cfg.get("timeout_seconds", cfg.get("timeout", 2.0)), 2.0, 0.05, 20.0),
            max_output_tokens=_clamp_int(cfg.get("max_output_tokens", 220), 220, 64, 1200),
            max_operations=_clamp_int(cfg.get("max_operations", cfg.get("target_operations", 30)), 30, 1, 120),
            terminal_command_chars=_clamp_int(cfg.get("terminal_command_chars", 320), 320, 40, 4000),
            tool_arguments_chars=_clamp_int(cfg.get("tool_arguments_chars", 400), 400, 40, 4000),
            tool_result_chars=_clamp_int(cfg.get("tool_result_chars", 600), 600, 40, 8000),
            user_event_chars=_clamp_int(cfg.get("user_event_chars", 600), 600, 40, 8000),
            max_candidate_chars=_clamp_int(cfg.get("max_candidate_chars", 1200), 1200, 80, 8000),
            max_primary_chars=_clamp_int(cfg.get("max_primary_chars", 1200), 1200, 80, 8000),
            max_supplement_chars=_clamp_int(cfg.get("max_supplement_chars", 500), 500, 40, 4000),
            max_recent_supplements=_clamp_int(cfg.get("max_recent_supplements", 5), 5, 0, 20),
            cache_size=_clamp_int(cfg.get("cache_size", 256), 256, 0, 10000),
            policy_version=str(cfg.get("policy_version") or DEFAULT_POLICY_VERSION),
            prompt_version=str(cfg.get("prompt_version") or STATUS_GUARD_SCHEMA_VERSION),
            judge_prompt=str(cfg.get("judge_prompt") or cfg.get("prompt") or DEFAULT_STATUS_GUARD_JUDGE_PROMPT),
        )

    def signature(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))


@dataclass
class PreSendStatusGuardDecision:
    decision: str = "allow"
    reason: str = ""
    unsupported_claims: List[str] = None  # type: ignore[assignment]
    steer_prompt: str = ""
    raw_payload: Dict[str, Any] = None  # type: ignore[assignment]
    fallback_reason: str = ""

    def __post_init__(self) -> None:
        if self.decision not in _DECISIONS:
            self.decision = "allow"
        if self.unsupported_claims is None:
            self.unsupported_claims = []
        if self.raw_payload is None:
            self.raw_payload = {}

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    @classmethod
    def from_payload(cls, payload: Optional[Dict[str, Any]], *, fallback_reason: str = "") -> "PreSendStatusGuardDecision":
        data = payload or {}
        decision = str(data.get("decision") or "allow").strip()
        if decision not in _DECISIONS:
            decision = "allow"
        claims = data.get("unsupported_claims") or []
        if isinstance(claims, str):
            claims = [claims]
        elif not isinstance(claims, list):
            claims = []
        return cls(
            decision=decision,
            reason=str(data.get("reason") or ""),
            unsupported_claims=[str(item) for item in claims if str(item)],
            steer_prompt=str(data.get("steer_prompt") or ""),
            raw_payload=dict(data),
            fallback_reason=fallback_reason,
        )


def _iter_tool_calls(message: Mapping[str, Any]) -> Iterable[Dict[str, Any]]:
    calls = message.get("tool_calls") or []
    if isinstance(calls, str):
        parsed = _safe_json_loads(calls)
        calls = parsed if isinstance(parsed, list) else []
    if not isinstance(calls, list):
        calls = []
    for call in calls:
        if isinstance(call, Mapping):
            yield dict(call)


def _tool_call_name_and_args(call: Mapping[str, Any]) -> tuple[str, str]:
    raw_fn = call.get("function")
    fn: Mapping[str, Any] = raw_fn if isinstance(raw_fn, Mapping) else {}
    name = str(fn.get("name") or call.get("name") or call.get("tool_name") or "unknown")
    args = fn.get("arguments")
    if args is None:
        args = call.get("arguments") or call.get("input") or ""
    if not isinstance(args, str):
        try:
            args = json.dumps(args, ensure_ascii=False, sort_keys=True)
        except Exception:
            args = str(args)
    return name, args


def build_recent_operation_synopsis(
    messages: Iterable[Mapping[str, Any]],
    *,
    config: Optional[PreSendStatusGuardConfig] = None,
) -> List[Dict[str, Any]]:
    """Return a neutral, bounded recent-operation synopsis.

    This intentionally records what the runtime saw without deciding whether an
    operation is mutation, delivery, verification, or anything else.
    """
    cfg = config or PreSendStatusGuardConfig()
    operations: List[Dict[str, Any]] = []
    for idx, raw_msg in enumerate(messages or []):
        if not isinstance(raw_msg, Mapping):
            continue
        role = str(raw_msg.get("role") or "")
        seq = raw_msg.get("id") or raw_msg.get("seq") or idx
        for call in _iter_tool_calls(raw_msg):
            name, args = _tool_call_name_and_args(call)
            limit = cfg.terminal_command_chars if name == "terminal" else cfg.tool_arguments_chars
            operations.append({
                "seq": seq,
                "kind": "tool_call",
                "tool": name,
                "text": _clamp_redacted(args, limit),
            })
        if role == "tool":
            name = str(raw_msg.get("tool_name") or raw_msg.get("name") or raw_msg.get("recipient") or "tool")
            operations.append({
                "seq": seq,
                "kind": "tool_result",
                "tool": name,
                "text": _clamp_redacted(raw_msg.get("content") or "", cfg.tool_result_chars),
            })
        elif role == "user":
            content = str(raw_msg.get("content") or "")
            if "ASYNC DELEGATION" in content or "maximum number of tool-calling iterations" in content:
                operations.append({
                    "seq": seq,
                    "kind": "user_or_system_event",
                    "tool": "user_message",
                    "text": _clamp_redacted(content, cfg.user_event_chars),
                })
    return operations[-cfg.max_operations :]


def active_task_payload_from_task_intent(
    state: Optional[TaskIntentState],
    *,
    config: Optional[PreSendStatusGuardConfig] = None,
) -> Optional[Dict[str, Any]]:
    if state is None or state.status != "active":
        return None
    cfg = config or PreSendStatusGuardConfig()
    supplements = list(state.task_contract.raw_supplements or [])
    if cfg.max_recent_supplements > 0:
        supplements = supplements[-cfg.max_recent_supplements :]
    else:
        supplements = []
    return {
        "id": state.id,
        "kind": getattr(state, "kind", "") or "direct_message",
        "status": state.status,
        "raw_primary_text": _clamp_redacted(state.task_contract.raw_primary_text, cfg.max_primary_chars),
        "raw_supplements": [
            _clamp_redacted(item, cfg.max_supplement_chars)
            for item in supplements
            if str(item or "")
        ],
    }


def active_task_payload_from_goal(goal_state: Any, *, config: Optional[PreSendStatusGuardConfig] = None) -> Optional[Dict[str, Any]]:
    if goal_state is None or getattr(goal_state, "status", None) != "active":
        return None
    cfg = config or PreSendStatusGuardConfig()
    raw = str(getattr(goal_state, "task_contract", None) or getattr(goal_state, "goal", "") or "")
    supplements = []
    try:
        supplements = [str(item) for item in (getattr(goal_state, "subgoals", None) or [])]
    except Exception:
        supplements = []
    if cfg.max_recent_supplements > 0:
        supplements = supplements[-cfg.max_recent_supplements :]
    else:
        supplements = []
    return {
        "id": f"goal:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}",
        "kind": "goal",
        "status": getattr(goal_state, "status", "active"),
        "raw_primary_text": _clamp_redacted(raw, cfg.max_primary_chars),
        "raw_supplements": [
            _clamp_redacted(item, cfg.max_supplement_chars)
            for item in supplements
            if str(item or "")
        ],
    }


def build_status_guard_payload(
    *,
    candidate_response: str,
    active_task: Optional[Dict[str, Any]],
    messages: Iterable[Mapping[str, Any]],
    config: Optional[PreSendStatusGuardConfig] = None,
    source: str = "gateway",
    platform: str = "",
) -> Dict[str, Any]:
    cfg = config or PreSendStatusGuardConfig()
    return {
        "schema_version": cfg.prompt_version,
        "policy_version": cfg.policy_version,
        "source": source,
        "platform": platform,
        "active_task": active_task or {},
        "candidate_response": _clamp_redacted(candidate_response, cfg.max_candidate_chars),
        "recent_operations": build_recent_operation_synopsis(messages, config=cfg),
        "instructions": {
            "core_question": (
                "Does the candidate response make task-status claims unsupported by the active task "
                "and recent operations, or omit user-requested material solely because of uncertainty "
                "instead of answering with an explicit uncertainty or caveat?"
            ),
            "do_not_preclassify_actions": True,
            "operation_synopsis_is_neutral": True,
        },
    }


def build_status_guard_messages(
    payload: Dict[str, Any],
    *,
    config: Optional[PreSendStatusGuardConfig] = None,
) -> List[Dict[str, str]]:
    cfg = config or PreSendStatusGuardConfig()
    system = str(cfg.judge_prompt or DEFAULT_STATUS_GUARD_JUDGE_PROMPT)
    user = {
        "output_schema": {
            "decision": "allow | reject_and_steer",
            "unsupported_claims": [
                "short labels for unsupported status claims or requested omissions"
            ],
            "reason": "short reason grounded in operation numbers or the explicit omission",
            "steer_prompt": (
                "if rejected, concise instruction to continue verification, weaken the unsupported "
                "status claim, or address omitted requested material with an uncertainty caveat"
            ),
        },
        "input": payload,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False, separators=(",", ":"))},
    ]


def _sha256_json(data: Dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PreSendStatusGuard:
    def __init__(
        self,
        *,
        config: Optional[PreSendStatusGuardConfig] = None,
        llm_call: Callable[..., Any],
        cache: Optional[OrderedDict[str, PreSendStatusGuardDecision]] = None,
        cache_lock: Optional[Any] = None,
    ) -> None:
        self.config = config or PreSendStatusGuardConfig()
        self._llm_call = llm_call
        self._cache: OrderedDict[str, PreSendStatusGuardDecision] = cache if cache is not None else OrderedDict()
        self._cache_lock = cache_lock

    def cache_key(self, payload: Dict[str, Any]) -> str:
        return _sha256_json({"kind": "pre_send_status_guard", "payload": payload, "config": self.config.signature()})

    def judge(
        self,
        *,
        candidate_response: str,
        active_task: Optional[Dict[str, Any]],
        messages: Iterable[Mapping[str, Any]],
        source: str = "gateway",
        platform: str = "",
        use_cache: bool = True,
    ) -> Optional[PreSendStatusGuardDecision]:
        if not self.config.enabled:
            return None
        if not str(candidate_response or "").strip():
            return None
        payload = build_status_guard_payload(
            candidate_response=candidate_response,
            active_task=active_task,
            messages=messages,
            config=self.config,
            source=source,
            platform=platform,
        )
        key = self.cache_key(payload)
        cached = self._get_cached(key) if use_cache else None
        if cached is not None:
            return _clone_decision(cached, cache_status="hit")
        messages_payload = build_status_guard_messages(payload, config=self.config)
        started = time.monotonic()
        try:
            response = self._llm_call(
                messages=messages_payload,
                timeout=self.config.timeout_seconds,
                max_tokens=self.config.max_output_tokens,
            )
            parsed = _extract_json_object(_response_text(response))
            decision = PreSendStatusGuardDecision.from_payload(parsed)
            if not parsed:
                decision.fallback_reason = "status guard returned invalid or empty JSON"
                decision.raw_payload.setdefault("invalid_judge_json", True)
            decision.raw_payload.setdefault("cache", "miss")
            decision.raw_payload.setdefault("latency_ms", int((time.monotonic() - started) * 1000))
            decision.raw_payload.setdefault("cache_key", key)
        except Exception as exc:
            return PreSendStatusGuardDecision(
                decision="allow",
                reason="status guard failed open",
                raw_payload={"cache": "uncached_error", "cache_key": key, "error_type": type(exc).__name__},
                fallback_reason="status guard failed open",
            )
        if use_cache:
            self._store_cache(key, decision)
        return _clone_decision(decision, cache_status="miss")

    def _get_cached(self, key: str) -> Optional[PreSendStatusGuardDecision]:
        lock = self._cache_lock
        if lock is not None:
            with lock:
                cached = self._cache.get(key)
                if cached is None:
                    return None
                self._cache.move_to_end(key)
                return cached
        cached = self._cache.get(key)
        if cached is None:
            return None
        self._cache.move_to_end(key)
        return cached

    def _store_cache(self, key: str, decision: PreSendStatusGuardDecision) -> None:
        if self.config.cache_size <= 0:
            return
        lock = self._cache_lock
        if lock is not None:
            with lock:
                self._store_cache_unlocked(key, decision)
            return
        self._store_cache_unlocked(key, decision)

    def _store_cache_unlocked(self, key: str, decision: PreSendStatusGuardDecision) -> None:
        self._cache[key] = _clone_decision(decision, cache_status="stored")
        self._cache.move_to_end(key)
        while len(self._cache) > self.config.cache_size:
            self._cache.popitem(last=False)


def _clone_decision(
    decision: PreSendStatusGuardDecision,
    *,
    cache_status: Optional[str] = None,
) -> PreSendStatusGuardDecision:
    raw_payload = dict(decision.raw_payload or {})
    if cache_status is not None:
        raw_payload["cache"] = cache_status
    return PreSendStatusGuardDecision(
        decision=decision.decision,
        reason=decision.reason,
        unsupported_claims=list(decision.unsupported_claims or []),
        steer_prompt=decision.steer_prompt,
        raw_payload=raw_payload,
        fallback_reason=decision.fallback_reason,
    )


__all__ = [
    "PreSendStatusGuard",
    "PreSendStatusGuardConfig",
    "PreSendStatusGuardDecision",
    "active_task_payload_from_goal",
    "active_task_payload_from_task_intent",
    "build_recent_operation_synopsis",
    "build_status_guard_messages",
    "build_status_guard_payload",
    "DEFAULT_STATUS_GUARD_JUDGE_PROMPT",
]
