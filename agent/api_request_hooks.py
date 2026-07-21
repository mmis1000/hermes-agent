"""Lifecycle-hook payloads for ``AIAgent`` API requests.

JSON-safe coercion, secret-key redaction, size caps, and the ``api_request_error`` hook dispatch.
Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s MRO unchanged.
"""
import math
import json
import os
import time
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, Dict, Optional

from agent.usage_pricing import normalize_usage

_SENSITIVE_HOOK_KEYS = {"api_key", "authorization", "proxy_authorization", "cookie", "set_cookie"}


def _model_dump(value: Any) -> Any:
    """``value.model_dump(mode="json")`` with graceful degradation for older pydantic signatures.

    warnings=False: pydantic UserWarnings on generic-union SDK models would leak to the terminal.
    """
    try:
        return value.model_dump(mode="json", warnings=False)
    except TypeError:
        try:
            return value.model_dump(mode="json")
        except TypeError:
            return value.model_dump()


class ApiRequestHooksMixin:
    """Hook payload sanitising + ``api_request_error`` dispatch (see module docstring)."""

    def _next_token_usage_session_event_index(
        self,
        *,
        core_includes_current: bool = False,
    ) -> int:
        """Advance the one session-wide counter used by usage hook producers.

        The standard API loop emits before its normal accounting increment,
        while the Codex terminal fallback runs after core accounting already
        included the turn. Anchoring both paths to this private counter keeps
        notification/fallback/ordinary sequences monotonic without changing
        the existing session accounting authority.
        """
        current = getattr(self, "_token_usage_hook_session_events", None)
        if not isinstance(current, int) or isinstance(current, bool) or current < 0:
            current = 0
        core_count = getattr(self, "session_api_calls", 0)
        if not isinstance(core_count, int) or isinstance(core_count, bool):
            try:
                core_count = int(core_count or 0)
            except (TypeError, ValueError, OverflowError):
                core_count = 0
        core_baseline = max(core_count - (1 if core_includes_current else 0), 0)
        current = max(current, core_baseline) + 1
        self._token_usage_hook_session_events = current
        return current

    @staticmethod
    def _coerce_token_usage_hook_int(value: Any) -> int:
        """Reject malformed provider token counts at the hook boundary."""
        if value is None or value == "" or isinstance(value, bool):
            return 0
        if isinstance(value, float) and not math.isfinite(value):
            return 0
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        return parsed if parsed >= 0 else 0

    def _invoke_token_usage_hook(
        self,
        *,
        task_id: str = "",
        turn_id: str = "",
        api_request_id: Optional[str] = None,
        api_call_count: int = 0,
        session_api_call_count: Optional[int] = None,
        usage: Optional[Dict[str, Any]] = None,
        raw_usage: Any = None,
        source: str = "api_response",
        started_at: Optional[float] = None,
        ended_at: Optional[float] = None,
        api_duration: Optional[float] = None,
        finish_reason: Optional[str] = None,
        response_model: Optional[str] = None,
        response: Any = None,
        assistant_message: Any = None,
        _hook_available: bool = False,
        advance_session_event: bool = False,
        core_includes_current: bool = False,
        **extra: Any,
    ) -> None:
        """Emit one provider-response event through ``post_api_request``.

        ``post_api_request`` is the current per-request observer contract, so
        alternate runtimes reuse it rather than introducing a parallel hook.
        Payloads are best-effort, bounded, secret-redacted, and JSON-safe; a
        plugin failure must never fail an otherwise completed model response.
        """
        try:
            from hermes_cli import plugins as _plugins

            if not _hook_available and not _plugins.has_hook("post_api_request"):
                return

            if advance_session_event:
                session_api_call_count = self._next_token_usage_session_event_index(
                    core_includes_current=core_includes_current,
                )

            usage_payload = self._sanitize_hook_payload(usage or {})
            if isinstance(usage_payload, dict):
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "reported_total_tokens",
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                ):
                    if key in usage_payload:
                        usage_payload[key] = self._coerce_token_usage_hook_int(
                            usage_payload.get(key)
                        )
            raw_usage_payload = (
                self._sanitize_hook_payload(raw_usage)
                if raw_usage is not None
                else None
            )
            kwargs: Dict[str, Any] = {
                "task_id": task_id,
                "turn_id": turn_id,
                "api_request_id": api_request_id or turn_id,
                "session_id": self.session_id or "",
                "platform": self.platform or "",
                "model": self.model,
                "provider": self.provider,
                "base_url": self.base_url,
                "api_mode": self.api_mode,
                "source": source,
                "api_call_count": api_call_count,
                "session_api_call_count": (
                    session_api_call_count
                    if session_api_call_count is not None
                    else getattr(self, "session_api_calls", 0)
                ),
                "finish_reason": finish_reason,
                "response_model": response_model,
                "usage": usage_payload,
                "raw_usage": raw_usage_payload,
            }
            if isinstance(usage_payload, dict):
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "estimated_cost_usd",
                    "cost_status",
                    "cost_source",
                ):
                    if key in usage_payload:
                        kwargs[key] = usage_payload.get(key)
            if started_at is not None:
                kwargs["started_at"] = started_at
            if ended_at is not None:
                kwargs["ended_at"] = ended_at
            if api_duration is not None:
                kwargs["api_duration"] = api_duration
            if response is not None:
                kwargs["response"] = response
            if assistant_message is not None:
                # Preserve the established post_api_request payload for
                # subscribers such as Langfuse that serialize this object.
                kwargs["assistant_message"] = assistant_message
            for key, value in extra.items():
                if value is not None:
                    kwargs[key] = self._sanitize_hook_payload(value)
            _plugins.invoke_hook("post_api_request", **kwargs)
        except Exception:
            pass

    def _usage_summary_for_api_request_hook(self, response: Any) -> Optional[Dict[str, Any]]:
        """Token buckets for ``post_api_request`` plugins (no raw ``response`` object)."""
        if response is None:
            return None
        raw_usage = getattr(response, "usage", None)
        if not raw_usage:
            return None
        from dataclasses import asdict

        cu = normalize_usage(raw_usage, provider=self.provider, api_mode=self.api_mode)
        summary = asdict(cu)
        summary.pop("raw_usage", None)
        summary["prompt_tokens"] = cu.prompt_tokens
        summary["total_tokens"] = cu.total_tokens
        return summary

    @staticmethod
    def _hook_payload_max_chars() -> int:
        raw = os.getenv("HERMES_PLUGIN_PAYLOAD_MAX_CHARS", "50000")
        try:
            return max(1000, int(raw))
        except (TypeError, ValueError):
            return 50000

    @staticmethod
    def _is_sensitive_hook_key(key: Any) -> bool:
        if not isinstance(key, str):
            return False
        lowered = key.lower().replace("-", "_")
        return lowered in _SENSITIVE_HOOK_KEYS or lowered.endswith("_api_key")

    @classmethod
    def _hook_jsonable(
        cls, value: Any, *, depth: int = 0, max_depth: int = 8, max_string: int = 8000,
        max_sequence: int = 200,
    ) -> Any:
        if depth > max_depth:
            return f"<{type(value).__name__} depth limit>"
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, str):
            if len(value) > max_string:
                return value[:max_string] + f"...[truncated {len(value) - max_string} chars]"
            return value
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"

        def recurse(item):
            return cls._hook_jsonable(
                item, depth=depth + 1, max_depth=max_depth, max_string=max_string,
                max_sequence=max_sequence,
            )

        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for idx, (key, item) in enumerate(value.items()):
                if idx >= max_sequence:
                    out["_truncated_items"] = len(value) - max_sequence
                    break
                str_key = str(key)
                out[str_key] = "<redacted>" if cls._is_sensitive_hook_key(str_key) else recurse(item)
            return out
        if isinstance(value, (list, tuple, set)):
            seq = list(value)
            out = [recurse(item) for item in seq[:max_sequence]]
            if len(seq) > max_sequence:
                out.append({"_truncated_items": len(seq) - max_sequence})
            return out
        with suppress(Exception):
            if hasattr(value, "model_dump"):
                return recurse(_model_dump(value))
        with suppress(Exception):
            from dataclasses import asdict, is_dataclass
            if is_dataclass(value):
                return recurse(asdict(value))
        if isinstance(value, SimpleNamespace):
            return recurse(vars(value))
        if hasattr(value, "__dict__"):
            with suppress(Exception):
                return recurse({k: v for k, v in vars(value).items() if not str(k).startswith("_")})
        return str(value)[:max_string]

    @classmethod
    def _sanitize_hook_payload(cls, value: Any) -> Any:
        """JSON-able payload under the size cap: full → reduced caps → truncated preview."""
        limit = cls._hook_payload_max_chars()
        encoded = ""
        for caps in ({}, {"max_string": 1000, "max_sequence": 50}):
            payload = cls._hook_jsonable(value, **caps)
            try:
                encoded = json.dumps(payload, ensure_ascii=False, default=str)
            except Exception:
                return str(payload)[:limit]
            if len(encoded) <= limit:
                return payload
        return {
            "_truncated": True, "original_type": type(value).__name__, "preview": encoded[:limit]
        }

    def _api_request_payload_for_hook(self, api_kwargs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        body = {
            key: value
            for key, value in (api_kwargs or {}).items()
            if key not in {"timeout", "http_client"}
        }
        return self._sanitize_hook_payload({"method": "POST", "body": body})

    def _api_response_payload_for_hook(
        self, response: Any, assistant_message: Any, *, finish_reason: Optional[str], usage_summary: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        # Raw provider SDK tool_call objects are handed to the sanitizer on purpose; `_hook_jsonable` must
        # keep normalising them (model_dump / __dict__ / dataclass) or subscribers get str() blobs.
        tool_calls = getattr(assistant_message, "tool_calls", None) or []
        return self._sanitize_hook_payload(
            {
                "model": getattr(response, "model", None),
                # Downstream that served the call (relays re-roll it per request; #90216).
                "upstream_provider": getattr(response, "provider", None),
                "finish_reason": finish_reason,
                "assistant_message": {
                    "role": getattr(assistant_message, "role", "assistant"),
                    "content": getattr(assistant_message, "content", None),
                    "tool_calls": tool_calls,
                },
                "usage": usage_summary if usage_summary is not None else self._usage_summary_for_api_request_hook(response),
            }
        )

    def _invoke_api_request_error_hook(
        self, *, task_id: str, turn_id: str, api_request_id: str, api_call_count: int,
        api_start_time: float, api_kwargs: Optional[Dict[str, Any]], error_type: str,
        error_message: str, status_code: Optional[int] = None, retry_count: Optional[int] = None,
        max_retries: Optional[int] = None, retryable: Optional[bool] = None,
        reason: Optional[str] = None,
    ) -> None:
        # Lazy module import (not from-import) so tests can replace lifecycle dispatch at this call site.
        with suppress(Exception):
            from hermes_cli import lifecycle as _lifecycle
            if not _lifecycle.has_hook("api_request_error"):
                return
            ended_at = time.time()
            _lifecycle.invoke_hook(
                "api_request_error",
                task_id=task_id,
                turn_id=turn_id,
                api_request_id=api_request_id,
                session_id=self.session_id or "",
                platform=self.platform or "",
                model=self.model,
                provider=self.provider,
                base_url=self.base_url,
                api_mode=self.api_mode,
                api_call_count=api_call_count,
                api_duration=ended_at - api_start_time,
                started_at=api_start_time,
                ended_at=ended_at,
                status_code=status_code,
                retry_count=retry_count,
                max_retries=max_retries,
                retryable=retryable,
                reason=reason,
                error={"type": error_type, "message": error_message},
                request=self._api_request_payload_for_hook(api_kwargs),
            )
