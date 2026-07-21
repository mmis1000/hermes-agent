"""Bounded review and continuation helpers for gateway iteration exhaustion.

The judge is deliberately a small auxiliary classifier: no tools, no full
``AIAgent``, a fixed-size transcript excerpt, and strict JSON.  Raw user wording
is carried separately and never rewritten by the judge.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

ITERATION_CONTROL_FLAG = "_iteration_control_synthetic"
ITERATION_CONTROL_METADATA = {
    ITERATION_CONTROL_FLAG: True,
    "task_intent_source": "iteration_control",
}
DEFAULT_MAX_CONTINUATION_CHAIN = 8

_MAX_GOAL_REVIEW_CHARS = 4_000
_MAX_SUMMARY_CHARS = 2_000
_MAX_EXCERPT_MESSAGES = 14
_MAX_EXCERPT_ITEM_CHARS = 600
_MAX_REASON_CHARS = 500

_SYSTEM_PROMPT = (
    "You are a small continuation-safety classifier. Hermes exhausted one "
    "tool-calling iteration budget. Decide whether the shown work is making "
    "concrete forward progress and can continue automatically, or whether it "
    "is stuck, repetitive, blocked, or needs user input. Return exactly one "
    "JSON object and no markdown. Do not rewrite the user's goal."
)


@dataclass(frozen=True)
class IterationContinuationVerdict:
    decision: str
    reason: str


@dataclass(frozen=True)
class IterationContinuation:
    prompt: str
    raw_goal: str
    persist_metadata: dict[str, Any]


def _head_tail(value: Any, limit: int) -> str:
    """Bound text without paraphrasing its surviving bytes."""

    text = str(value or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    head = (limit - 1) // 2
    tail = limit - head - 1
    return text[:head] + "…" + text[-tail:]


def is_iteration_control_message(message: Mapping[str, Any]) -> bool:
    return bool(message.get(ITERATION_CONTROL_FLAG))


def latest_real_user_goal(messages: Iterable[Mapping[str, Any]]) -> str:
    """Return the latest non-synthetic user text exactly as stored."""

    materialized = list(messages or [])
    for message in reversed(materialized):
        if not isinstance(message, Mapping):
            continue
        if message.get("role") != "user" or is_iteration_control_message(message):
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            return content
    return ""


def _tool_names(message: Mapping[str, Any]) -> str:
    names: list[str] = []
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return ""
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        function = call.get("function")
        if not isinstance(function, Mapping):
            function = {}
        name = function.get("name") or call.get("name")
        if name:
            names.append(str(name))
    return ", ".join(names)


def build_recent_review_excerpt(
    messages: Iterable[Mapping[str, Any]],
    *,
    max_messages: int = _MAX_EXCERPT_MESSAGES,
    item_chars: int = _MAX_EXCERPT_ITEM_CHARS,
) -> str:
    """Build a bounded recent transcript excerpt without synthetic controls."""

    eligible = [
        message
        for message in (messages or [])
        if isinstance(message, Mapping) and not is_iteration_control_message(message)
    ]
    if max_messages <= 0:
        return ""
    lines: list[str] = []
    for message in eligible[-max_messages:]:
        role = str(message.get("role") or "unknown")
        names = _tool_names(message)
        if role == "assistant" and names:
            content = f"tool_calls={names}"
        else:
            content = _head_tail(message.get("content"), item_chars) or "(empty)"
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    try:
        return str(response.choices[0].message.content or "")
    except Exception:
        return ""


def _parse_strict_verdict(raw: Any) -> IterationContinuationVerdict:
    text = _response_text(raw).strip()
    if not text:
        return IterationContinuationVerdict("ask_user", "continuation judge returned an empty verdict")
    try:
        payload = json.loads(text)
    except Exception:
        return IterationContinuationVerdict("ask_user", "continuation judge returned malformed JSON")
    if not isinstance(payload, dict) or set(payload) != {"decision", "reason"}:
        return IterationContinuationVerdict("ask_user", "continuation judge returned the wrong JSON schema")
    decision = payload.get("decision")
    reason = payload.get("reason")
    if decision not in {"auto_continue", "ask_user"} or not isinstance(reason, str):
        return IterationContinuationVerdict("ask_user", "continuation judge returned an invalid verdict")
    reason = _head_tail(reason, _MAX_REASON_CHARS)
    if not reason:
        return IterationContinuationVerdict("ask_user", "continuation judge gave no reason")
    return IterationContinuationVerdict(decision, reason)


def judge_iteration_exhaustion(
    *,
    result: Mapping[str, Any],
    raw_goal: str,
    llm_call: Callable[..., Any],
    model: str,
    main_runtime: Mapping[str, Any],
) -> IterationContinuationVerdict:
    """Run the bounded, toolless auxiliary continuation judge.

    Any exception or malformed output conservatively asks the user.
    """

    messages = result.get("messages")
    if not isinstance(messages, list):
        messages = []
    payload = {
        "output_schema": {
            "decision": "auto_continue | ask_user",
            "reason": "short string",
        },
        "turn_exit_reason": _head_tail(result.get("turn_exit_reason"), 160),
        "api_calls": int(result.get("api_calls") or 0),
        "latest_real_user_raw_goal": _head_tail(raw_goal, _MAX_GOAL_REVIEW_CHARS),
        "forced_summary": _head_tail(result.get("final_response"), _MAX_SUMMARY_CHARS),
        "recent_transcript": build_recent_review_excerpt(messages),
    }
    request_messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]
    try:
        response = llm_call(
            task="task_intent",
            model=model,
            messages=request_messages,
            temperature=0,
            max_tokens=180,
            tools=None,
            timeout=5.0,
            main_runtime=dict(main_runtime or {}),
        )
    except Exception as exc:
        return IterationContinuationVerdict(
            "ask_user",
            f"continuation judge failed: {type(exc).__name__}",
        )
    return _parse_strict_verdict(response)


def build_iteration_continuation(
    *,
    raw_goal: str,
    user_approved: bool,
) -> IterationContinuation:
    """Create a marked synthetic turn while preserving the exact raw goal."""

    approval = (
        "The user approved continuing after the safety review."
        if user_approved
        else "A bounded safety review approved automatic continuation."
    )
    prompt = (
        "[Iteration-control message — synthetic, not a new user task]\n"
        "The previous turn hit its iteration budget. "
        f"{approval} Continue from the saved transcript without repeating the "
        "forced summary. Finish the existing task, or stop and ask for input if "
        "you are blocked.\n\n"
        "Exact active user goal (preserved verbatim):\n"
        "<raw-user-goal>\n"
        f"{raw_goal}\n"
        "</raw-user-goal>"
    )
    return IterationContinuation(
        prompt=prompt,
        raw_goal=raw_goal,
        persist_metadata=dict(ITERATION_CONTROL_METADATA),
    )


__all__ = [
    "DEFAULT_MAX_CONTINUATION_CHAIN",
    "ITERATION_CONTROL_FLAG",
    "ITERATION_CONTROL_METADATA",
    "IterationContinuation",
    "IterationContinuationVerdict",
    "build_iteration_continuation",
    "build_recent_review_excerpt",
    "is_iteration_control_message",
    "judge_iteration_exhaustion",
    "latest_real_user_goal",
]
