"""token_usage_report — local Markdown/JSONL token usage reporter.

The plugin listens to Hermes' ``post_api_request`` hook, appends one JSONL row
per model request, and rewrites a compact Markdown report.  It is intentionally
file-only: no external service, no optional dependency, and no config reads on
the hot path beyond environment variables.

Output defaults to::

    ~/.hermes/reports/token_usage/events.jsonl
    ~/.hermes/reports/token_usage/latest.md

Environment knobs:
    HERMES_TOKEN_USAGE_REPORT_DIR          Override output directory.
    HERMES_TOKEN_USAGE_REPORT_MAX_EVENTS   Events to scan for latest.md (default 20000).
    HERMES_TOKEN_USAGE_REPORT_RECENT_ROWS  Recent events in report (default 25).
    HERMES_TOKEN_USAGE_REPORT_TARGETS      Exact reasoning-token targets (default 516,1034,1552).
"""
from __future__ import annotations

import json
import math
import os
import statistics
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from hermes_constants import get_hermes_home

try:  # POSIX cross-process serialization; thread-only fallback elsewhere.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None

_LOCK = threading.Lock()
_EVENTS_FILE = "events.jsonl"
_REPORT_FILE = "latest.md"
_LOCK_FILE = ".token_usage_report.lock"
_MAX_TAIL_SCAN_BYTES = 64 * 1024 * 1024
_DEFAULT_TARGETS = (516, 1034, 1552)


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int = 0) -> int:
    if value is None or value == "" or isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed >= 0 else default


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _utc_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat(timespec="seconds")


def _report_dir() -> Path:
    override = os.environ.get("HERMES_TOKEN_USAGE_REPORT_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(get_hermes_home()) / "reports" / "token_usage"


def _targets() -> tuple[int, ...]:
    raw = os.environ.get("HERMES_TOKEN_USAGE_REPORT_TARGETS", "").strip()
    if not raw:
        return _DEFAULT_TARGETS
    targets: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value > 0:
            targets.append(value)
    return tuple(targets) or _DEFAULT_TARGETS


def _usage_dict(kwargs: dict[str, Any]) -> dict[str, Any]:
    usage = kwargs.get("usage")
    return usage if isinstance(usage, dict) else {}


def _raw_usage_dict(kwargs: dict[str, Any]) -> dict[str, Any]:
    raw = kwargs.get("raw_usage")
    return raw if isinstance(raw, dict) else {}


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return str(value)


def _pick_int(kwargs: dict[str, Any], usage: dict[str, Any], raw_usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        if key in kwargs:
            return _coerce_int(kwargs.get(key))
        if key in usage:
            return _coerce_int(usage.get(key))
        if key in raw_usage:
            return _coerce_int(raw_usage.get(key))
    return 0


def _event_from_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    usage = _usage_dict(kwargs)
    raw_usage = _raw_usage_dict(kwargs)
    now = time.time()
    event: dict[str, Any] = {
        "timestamp": now,
        "iso_time": _utc_iso(now),
        "session_id": str(kwargs.get("session_id") or ""),
        "turn_id": str(kwargs.get("turn_id") or ""),
        "api_request_id": str(kwargs.get("api_request_id") or ""),
        "source": str(kwargs.get("source") or ""),
        "platform": str(kwargs.get("platform") or ""),
        "model": str(kwargs.get("model") or ""),
        "response_model": str(kwargs.get("response_model") or ""),
        "provider": str(kwargs.get("provider") or ""),
        "api_mode": str(kwargs.get("api_mode") or ""),
        "finish_reason": str(kwargs.get("finish_reason") or ""),
        "cost_status": str(kwargs.get("cost_status") or usage.get("cost_status") or ""),
        "cost_source": str(kwargs.get("cost_source") or usage.get("cost_source") or ""),
    }
    event["input_tokens"] = _pick_int(kwargs, usage, raw_usage, "input_tokens", "inputTokens")
    event["cache_read_tokens"] = _pick_int(kwargs, usage, raw_usage, "cache_read_tokens", "cachedInputTokens")
    event["cache_write_tokens"] = _pick_int(kwargs, usage, raw_usage, "cache_write_tokens", "cacheCreationInputTokens")
    event["output_tokens"] = _pick_int(kwargs, usage, raw_usage, "output_tokens", "outputTokens")
    event["reasoning_tokens"] = _pick_int(kwargs, usage, raw_usage, "reasoning_tokens", "reasoningOutputTokens")
    event["prompt_tokens"] = _pick_int(kwargs, usage, raw_usage, "prompt_tokens") or (
        event["input_tokens"] + event["cache_read_tokens"] + event["cache_write_tokens"]
    )
    event["completion_tokens"] = _pick_int(kwargs, usage, raw_usage, "completion_tokens") or event["output_tokens"]
    event["total_tokens"] = _pick_int(kwargs, usage, raw_usage, "total_tokens")
    event["reported_total_tokens"] = _pick_int(kwargs, usage, raw_usage, "reported_total_tokens", "totalTokens")
    if not event["total_tokens"]:
        event["total_tokens"] = event["prompt_tokens"] + event["completion_tokens"]
    if not event["reported_total_tokens"]:
        event["reported_total_tokens"] = event["total_tokens"]
    event["api_call_count"] = _pick_int(kwargs, usage, raw_usage, "api_call_count")
    event["session_api_call_count"] = _pick_int(kwargs, usage, raw_usage, "session_api_call_count")
    cost = _coerce_float(kwargs.get("estimated_cost_usd") or usage.get("estimated_cost_usd"))
    if cost is not None:
        event["estimated_cost_usd"] = cost
    if kwargs.get("codex_thread_id"):
        event["codex_thread_id"] = str(kwargs.get("codex_thread_id"))
    if kwargs.get("codex_turn_id"):
        event["codex_turn_id"] = str(kwargs.get("codex_turn_id"))
    # Keep raw usage because it is the only source of Codex's exact app-server
    # field names; cap remains controlled by the core hook sanitizer.
    if raw_usage:
        event["raw_usage"] = _json_safe_value(raw_usage)
    return event


@contextmanager
def _output_lock(out_dir: Path) -> Iterator[None]:
    """Serialize event/report updates across threads and POSIX processes."""
    with _LOCK:
        out_dir.mkdir(parents=True, exist_ok=True)
        if _fcntl is None:
            yield
            return
        with (out_dir / _LOCK_FILE).open("a+", encoding="utf-8") as lock_file:
            _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(row + "\n")


def _load_recent_events(path: Path, max_events: int) -> list[dict[str, Any]]:
    """Read only the bounded tail needed for the current report."""
    if not path.exists():
        return []

    max_events = max(1, int(max_events))
    # Read a few extra physical rows so one interrupted/malformed append does
    # not unnecessarily shrink the valid report window.
    max_lines = max_events + 100
    chunks: list[bytes] = []
    newline_count = 0
    bytes_scanned = 0
    chunk_size = 64 * 1024
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        position = fh.tell()
        while (
            position > 0
            and newline_count <= max_lines
            and bytes_scanned < _MAX_TAIL_SCAN_BYTES
        ):
            size = min(
                chunk_size,
                position,
                _MAX_TAIL_SCAN_BYTES - bytes_scanned,
            )
            position -= size
            fh.seek(position)
            chunk = fh.read(size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
            bytes_scanned += len(chunk)

    raw_lines = b"".join(reversed(chunks)).splitlines()
    if position > 0 and raw_lines:
        # The first row begins before the bounded read window.
        raw_lines = raw_lines[1:]

    rows: deque[dict[str, Any]] = deque(maxlen=max_events)
    for raw_line in raw_lines[-max_lines:]:
        try:
            obj = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return list(rows)


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * pct)
    return ordered[max(0, min(index, len(ordered) - 1))]


def _fmt_int(value: Any) -> str:
    return f"{_coerce_int(value):,}"


def _fmt_ratio(numer: int, denom: int) -> str:
    if denom <= 0:
        return "—"
    return f"{100.0 * numer / denom:.1f}%"


def _markdown_cell(value: Any) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\r", "<br>")
        .replace("\n", "<br>")
    )


def _table(headers: Iterable[str], rows: Iterable[Iterable[Any]]) -> str:
    header_list = [_markdown_cell(header) for header in headers]
    out = ["| " + " | ".join(header_list) + " |"]
    out.append("| " + " | ".join("---" for _ in header_list) + " |")
    for row in rows:
        out.append("| " + " | ".join(_markdown_cell(cell) for cell in row) + " |")
    return "\n".join(out)


def _model_name(event: dict[str, Any]) -> str:
    return str(event.get("response_model") or event.get("model") or "unknown")


def _render_report(events: list[dict[str, Any]], targets: tuple[int, ...]) -> str:
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_model[_model_name(event)].append(event)

    lines: list[str] = [
        "# Hermes token usage report",
        "",
        f"Updated: {_utc_iso()}",
        f"Events in window: {len(events):,}",
        "",
    ]

    model_rows = []
    for model, model_events in sorted(by_model.items(), key=lambda item: (-len(item[1]), item[0])):
        reasoning = [_coerce_int(e.get("reasoning_tokens")) for e in model_events]
        total_reasoning = sum(reasoning)
        model_rows.append(
            [
                model,
                f"{len(model_events):,}",
                f"{len({e.get('session_id') for e in model_events if e.get('session_id')}):,}",
                _fmt_int(total_reasoning),
                _fmt_int(round(statistics.mean(reasoning)) if reasoning else 0),
                _fmt_int(_percentile(reasoning, 0.50)),
                _fmt_int(_percentile(reasoning, 0.90)),
                _fmt_int(max(reasoning) if reasoning else 0),
            ]
        )
    lines.extend([
        "## By model",
        "",
        _table(
            ["Model", "Events", "Sessions", "Reasoning total", "Mean", "P50", "P90", "Max"],
            model_rows or [["—", "0", "0", "0", "0", "0", "0", "0"]],
        ),
        "",
    ])

    target_rows = []
    for model, model_events in sorted(by_model.items(), key=lambda item: item[0]):
        reasoning = [_coerce_int(e.get("reasoning_tokens")) for e in model_events]
        for target in targets:
            exact = sum(1 for value in reasoning if value == target)
            at_least = sum(1 for value in reasoning if value >= target)
            target_rows.append([model, _fmt_int(target), _fmt_int(exact), _fmt_int(at_least), _fmt_ratio(exact, at_least)])
    lines.extend([
        "## Fixed-boundary reasoning-token clustering",
        "",
        _table(
            ["Model", "Target", "Exact", ">= target", "Exact / >= target"],
            target_rows or [["—", "—", "0", "0", "—"]],
        ),
        "",
    ])

    recent_rows = []
    recent_count = _env_int("HERMES_TOKEN_USAGE_REPORT_RECENT_ROWS", 25, minimum=1)
    for event in events[-recent_count:]:
        recent_rows.append(
            [
                str(event.get("iso_time") or ""),
                _model_name(event),
                str(event.get("session_id") or "")[-18:],
                _fmt_int(event.get("api_call_count")),
                _fmt_int(event.get("input_tokens")),
                _fmt_int(event.get("cache_read_tokens")),
                _fmt_int(event.get("output_tokens")),
                _fmt_int(event.get("reasoning_tokens")),
                _fmt_int(event.get("reported_total_tokens")),
            ]
        )
    lines.extend([
        "## Recent events",
        "",
        _table(
            ["UTC time", "Model", "Session suffix", "API #", "Input", "Cache read", "Output", "Reasoning", "Reported total"],
            recent_rows or [["—", "—", "—", "0", "0", "0", "0", "0", "0"]],
        ),
        "",
        "## Files",
        "",
        f"- JSONL events: `{_EVENTS_FILE}`",
        f"- Markdown report: `{_REPORT_FILE}`",
        "",
    ])
    return "\n".join(lines)


def _write_report(report_path: Path, events_path: Path) -> None:
    max_events = _env_int("HERMES_TOKEN_USAGE_REPORT_MAX_EVENTS", 20000, minimum=10)
    events = _load_recent_events(events_path, max_events)
    content = _render_report(events, _targets())
    report_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = report_path.with_suffix(report_path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(report_path)


def on_post_api_request(**kwargs: Any) -> None:
    event = _event_from_kwargs(kwargs)
    out_dir = _report_dir()
    with _output_lock(out_dir):
        events_path = out_dir / _EVENTS_FILE
        _append_event(events_path, event)
        _write_report(out_dir / _REPORT_FILE, events_path)


def register(ctx) -> None:
    ctx.register_hook("post_api_request", on_post_api_request)
