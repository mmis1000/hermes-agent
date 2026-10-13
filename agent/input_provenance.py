"""Host-authored input identities; text is data, never a source of authority.

Stored inside display_metadata, so existing transcript copy/restore and wire
stripping apply. Capturing an event does not admit it or invoke a plugin.
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
import uuid

logger = logging.getLogger(__name__)
KEY = "input_provenance"


class InputText(str):
    """In-process text handoff retaining immutable-copy input constituents."""
    input_provenance: list

    def __new__(cls, text, records):
        value = super().__new__(cls, text)
        value.input_provenance = copy.deepcopy(records)
        return value

    def __reduce__(self):
        return (type(self), (str(self), self.input_provenance))


def input_record(text, *, platform="", source_id="", message_id="", scope=(),
                 source_kind="unknown", platform_text=None, occurrence_id=None):
    identity = [v if isinstance(v, (str, int)) else "" for v in (platform, *scope, source_id, message_id)]
    uid = occurrence_id or (
        uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(identity, ensure_ascii=False)).hex
        if message_id else uuid.uuid4().hex
    )
    result = {"occurrence_id": uid, "text": text if isinstance(text, str) else "",
              "source_kind": source_kind, "source_id": source_id,
              "platform": platform, "message_id": message_id}
    if isinstance(platform_text, str):
        result["platform_text"] = platform_text
    return result


def event_inputs(event):
    """Return captured constituents without accepting any event.metadata claims."""
    existing = getattr(event, "_input_provenance", None)
    if existing is not None:
        return copy.deepcopy(existing)
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", "")
    author = getattr(source, "user_id", None) or ""
    machine = bool(getattr(event, "internal", False) or getattr(source, "is_bot", False)
                   or not getattr(event, "allow_gateway_control", True))
    raw = getattr(event, "raw_message", None)
    # These platform shapes expose the user's text directly. Do not stringify
    # arbitrary SDK objects or call their repr (which may contain credentials).
    original = getattr(raw, "content", None) if platform == "discord" else None
    if platform == "telegram":
        original = getattr(raw, "text", None)
    result = [input_record(
        getattr(event, "text", ""), platform=platform, source_id=f"{platform}:{author}",
        message_id=str(getattr(event, "message_id", None) or ""),
        scope=tuple(getattr(source, k, None) or "" for k in
                    ("profile", "scope_id", "chat_id", "thread_id")),
        source_kind="machine" if machine else ("human" if source is not None else "unknown"),
        platform_text=original,
    )]
    event._input_provenance = copy.deepcopy(result)
    return result


def merge_event_inputs(target, other):
    left, right = event_inputs(target), event_inputs(other)
    seen = {r["occurrence_id"] for r in left}
    target._input_provenance = left + [r for r in right if r["occurrence_id"] not in seen]


def event_display_metadata(event):
    records = event_inputs(event)
    if getattr(event, "internal", False) or getattr(event, "_heartbeat_session_id", None):
        for record in records:
            record["source_kind"] = "machine"
    return {KEY: records}


def agent_input_context(agent):
    platform = getattr(agent, "platform", None) or "cli"
    role = "primary"
    if platform in {"cron", "subagent", "batch", "curator", "flush", "background_review"}:
        role = platform
    if getattr(agent, "_turn_origin", None) or getattr(agent, "side_agent", False) or getattr(agent, "_delegate_depth", 0):
        role = "auxiliary"
    from hermes_constants import get_hermes_home
    db = getattr(agent, "_session_db", None)
    home = str(Path(db.db_path).parent) if db is not None and isinstance(getattr(db, "db_path", None), (str, Path)) else str(get_hermes_home())
    return {"session_id": getattr(agent, "session_id", None) or "",
            "hermes_home": home, "agent_context": role, "platform": platform}


def notify_input(agent, message):
    """Notify an engine at admission, before any compression worker sees input."""
    engine = getattr(agent, "context_compressor", None)
    callback = getattr(engine, "on_input", None)
    if not callable(callback):
        return
    from agent.context_engine import ContextEngine
    if getattr(callback, "__func__", None) is ContextEngine.on_input:
        return
    try:
        callback(copy.deepcopy(message), **agent_input_context(agent))
    except Exception:
        logger.warning("Context engine input capture failed; task protection is degraded", exc_info=True)
        warn = getattr(agent, "_emit_warning", None)
        if callable(warn):
            warn("Context engine input capture failed; task protection is degraded.")


def prepare_local_input(agent, message, text):
    """Only a new direct local turn may mint provenance; never infer it on replay."""
    if KEY in (message.get("display_metadata") or {}):
        return
    context = agent_input_context(agent)
    local = context["platform"] in {"cli", "tui", "desktop", "acp"}
    if not local:
        return
    human = context["agent_context"] == "primary" and message.get("display_kind") in {None, "steer"}
    message.setdefault("display_metadata", {})[KEY] = [input_record(
        text, platform=context["platform"], source_kind="human" if human else "machine")]
