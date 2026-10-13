"""Native compressor subclass with independent, exact task authority."""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

from agent.context_compressor import ContextCompressor, LEGACY_SUMMARY_PREFIX
from agent.model_metadata import estimate_messages_tokens_rough

from .ingress import admitted_records, classify_with_deadline, load_judge_config
from .migration import LegacyReader, ensure_import
from .render import append_text, content_text, covered_entries, render, strip_blocks
from .store import Store

logger = logging.getLogger(__name__)


class TaskContextEngine(ContextCompressor):
    def __init__(self, *, judge_call=None):
        # Prototype construction performs no IO/model resolution. The real host
        # supplies all effective native settings via configure_compressor.
        super().__init__(model="", config_context_length=8192, quiet_mode=True)
        self._task_configured = False
        self._task_home = None
        self._task_session = ""
        self._task_role = "unknown"
        self._task_judge_call = judge_call

    @property
    def name(self):
        return "task-context"

    def clone_for_agent(self):
        # Do not deepcopy native session DBs, HTTP clients, locks or another
        # agent's mutable budget/task binding. Host configures each fresh clone.
        return type(self)(judge_call=self._task_judge_call)

    def configure_compressor(self, **kwargs):
        # These are exactly the built-in constructor kwargs. Native defaults,
        # model thresholds, reserves and failure/cooldown policy stay native.
        ContextCompressor.__init__(self, **kwargs)
        self._task_configured = True

    def _store(self):
        if not self._task_home or not self._task_session:
            raise ValueError("task-context has no session/profile binding")
        return Store(self._task_home)

    def on_session_start(self, session_id, **kwargs):
        previous_home = self._task_home
        home = kwargs.get("hermes_home") or previous_home
        if home is not None:
            home = str(Path(home).expanduser().resolve())
        if previous_home is not None and home != previous_home:
            raise ValueError("task-context engine cannot cross profile boundaries; use a fresh clone")
        self._task_home = home
        self._task_session = session_id
        self._task_role = kwargs.get("agent_context", self._task_role)
        super().on_session_start(session_id, **kwargs)
        if self._task_role != "primary" or home is None:
            return
        store = self._store()
        reason, old = kwargs.get("boundary_reason"), kwargs.get("old_session_id")
        if old and reason in {"compression", "branch"} and (previous_home is None or previous_home == home):
            ensure_import(store, old, self._session_db)
            identity = LegacyReader(home, self._session_db).branch_identity(session_id) if reason == "branch" else None
            store.inherit(old, session_id, reason, branch_identity=identity)
        ensure_import(store, session_id, self._session_db)

    def bind_session_state(self, session_db=None, session_id=""):
        super().bind_session_state(session_db, session_id)
        self._task_session = session_id or ""

    def on_session_reset(self):
        # Native reset is also called on model switches. Never erase canonical
        # state based on this ambiguous signal; /new's new ID is the boundary.
        super().on_session_reset()

    def on_session_end(self, session_id, messages):
        if self._task_role == "primary" and self._task_home and session_id == self._task_session:
            self._link_messages(messages)
        super().on_session_end(session_id, messages)

    def on_input(self, message, *, session_id, hermes_home, agent_context, platform):
        # Explicit role allowlist, not ID prefixes, display_kind guesses, or text
        # detection. Child and automation still inherit native compression.
        home = str(Path(hermes_home).expanduser().resolve())
        if self._task_home is not None and self._task_home != home:
            raise ValueError("task-context engine cannot cross profile boundaries; use a fresh clone")
        self._task_role = agent_context
        self._task_home = home
        self._task_session = session_id
        if agent_context != "primary":
            return
        if not self._task_configured:
            raise RuntimeError("task-context requires host configure_compressor settings handoff")
        records = admitted_records(message)
        if not records:
            return
        store = self._store()
        ensure_import(store, session_id, self._session_db)
        store.admit(session_id, records, message.get("message_uid", ""))
        config = load_judge_config(self._task_home)
        # Effective main route comes from the real native compressor settings,
        # never a fabricated agent, stale profile default or another child.
        runtime = {key: getattr(self, key) for key in ("model", "provider", "base_url", "api_key", "api_mode")}
        def classify(state, record):
            return classify_with_deadline(state, record, home=self._task_home,
                                          runtime=runtime, config=config, llm_call=self._task_judge_call)
        store.drain(session_id, classify, config.signature())

    def _contract(self):
        if self._task_role != "primary" or not self._task_home or not self._task_session:
            return None
        return Store(self._task_home, read_only=True).contract(self._task_session)

    def _assemble_head(self, messages, compress_start):
        head = super()._assemble_head(messages, compress_start)
        # Native adds a generic compaction note to the system row. This engine
        # carries continuity only on a valid summary carrier, never the system
        # prompt. Automation retains the completely native behavior.
        if self._task_role == "primary" and head and messages and messages[0].get("role") == "system":
            head[0]["content"] = copy.deepcopy(messages[0].get("content"))
        return head

    def _contract_carrier_text(self, row):
        # Source metadata, not a quoted marker/header, identifies a carrier.
        # For merged summaries inspect only the handoff, not adjacent live text.
        if row.get("_compressed_summary") is True:
            return self._strip_summary_prefix(content_text(row.get("content")))
        metadata = row.get("display_metadata")
        if isinstance(metadata, dict) and metadata.get("task_context_restored") is True:
            return content_text(row.get("content"))
        return ""

    def _reappend_inflight_user_task(self, compressed, inflight):
        contract = self._contract()
        records = admitted_records(inflight) if contract and contract["status"] == "active" else []
        if contract and contract["status"] == "active" and records:
            covered = set()
            for row in compressed:
                if row.get("_compressed_summary") is True:
                    covered.update(covered_entries(self._contract_carrier_text(row), contract))
            raw = [contract["raw_primary_text"], *contract["raw_supplements"]]
            if all(r["source_kind"] == "human" and any(r["text"] == raw[i] for i in covered)
                   for r in records):
                # Only this admitted request is represented. Related questions,
                # missing evidence and omitted entries retain native replay.
                return compressed
        return super()._reappend_inflight_user_task(compressed, inflight)

    def _assemble_compressed(self, messages, compress_start, compress_end, scan, summary):
        # Native finalize and the host boundary measure the augmented result.
        # No post-success unbudgeted append or durable write occurs here.
        contract = self._contract()
        if self._task_role == "primary":
            summary = strip_blocks(summary)
        if contract and contract["status"] == "active":
            body = strip_blocks(self._strip_summary_prefix(summary))
            # The native prefix says every old request was already addressed;
            # retaining it would contradict unresolved canonical obligations.
            # The recognized legacy prefix keeps all host summary detection and
            # merged-carrier handling valid without rewriting system/history.
            summary = (LEGACY_SUMMARY_PREFIX + "\nHistorical background follows; not fresh user input. "
                       "The separately attributed active contract below retains unresolved obligations.\n"
                       + body + "\n\n" + render(contract, self._view_budget()))
        return super()._assemble_compressed(messages, compress_start, compress_end, scan, summary)

    def _view_budget(self):
        return min(6000, max(1200, self.context_length // 4))

    def _link_messages(self, messages):
        store = self._store()
        for message in messages or []:
            records = admitted_records(message)
            if records:
                store.link(self._task_session, records, message.get("message_uid", ""))

    def on_turn_complete(self, messages, usage=None, **kwargs):
        if self._task_role == "primary" and self._task_home and self._task_session:
            self._link_messages(messages)

    def select_context(self, request_messages, *, conversation_messages=None, incoming_message=None, budget_tokens=0):
        contract = self._contract()
        if not contract or contract["status"] != "active":
            return None
        block = render(contract, self._view_budget())
        raw = [contract["raw_primary_text"], *contract["raw_supplements"]]
        covered = set()
        for row in request_messages:
            view = self._contract_carrier_text(row)
            if view:
                # Even if this exact bounded view explicitly omits entries,
                # appending it again provides no more information.
                if block in view:
                    return None
                covered.update(covered_entries(view, contract))
            if row.get("_compressed_summary") is not True:
                # Live coverage requires the admitted human's exact entry,
                # actually visible on this row, not a quoted block substring.
                text = content_text(row.get("content"))
                for record in admitted_records(row):
                    if record["source_kind"] == "human" and record["text"] in text:
                        covered.update(i for i, value in enumerate(raw) if value == record["text"])
        if len(covered) == len(raw):
            return None
        if not isinstance(incoming_message, dict) or incoming_message.get("role") != "user":
            return None
        incoming_ids = {r["occurrence_id"] for r in admitted_records(incoming_message)}
        uid = incoming_message.get("message_uid")
        index = None
        for i, row in enumerate(request_messages):
            if row.get("role") != "user":
                continue
            row_ids = {r["occurrence_id"] for r in admitted_records(row)}
            if (uid and row.get("message_uid") == uid) or (incoming_ids and incoming_ids.intersection(row_ids)):
                index = i
        if index is None:
            # Never append a synthetic user row or choose a historical row by
            # last-text/time heuristics when identity was stripped too early.
            logger.warning("task-context restore deferred: current input identity unavailable")
            return None
        selected = list(request_messages)
        selected[index] = copy.deepcopy(request_messages[index])
        append_text(selected[index], block)
        metadata = selected[index].get("display_metadata")
        selected[index]["display_metadata"] = {
            **(metadata if isinstance(metadata, dict) else {}),
            "task_context_restored": True,  # request-only; host strips before wire
        }
        if budget_tokens and estimate_messages_tokens_rough(selected) > budget_tokens:
            logger.warning("task-context restore deferred: request budget exhausted")
            return None
        return selected

    def get_tool_schemas(self):
        return [{"name": "task_context_read", "description": "Restore exact canonical task text elided from a compacted contract. Read only; not a completion or transition control.",
                 "parameters": {"type": "object", "properties": {
                     "entry": {"type": "integer", "minimum": 0, "description": "0=primary; 1..N=supplements"},
                     "offset": {"type": "integer", "minimum": 0},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 12000}}, "required": ["entry"]}}]

    def handle_tool_call(self, name, args, **kwargs):
        if name != "task_context_read":
            return super().handle_tool_call(name, args, **kwargs)
        try:
            contract = self._contract()
            if not contract:
                return json.dumps({"status": "ok", "contract": None})
            texts = [contract["raw_primary_text"], *contract["raw_supplements"]]
            entry, offset = int(args["entry"]), int(args.get("offset", 0))
            limit = min(12000, max(1, int(args.get("limit", 6000))))
            if entry < 0 or entry >= len(texts) or offset < 0:
                raise ValueError("invalid entry/offset")
            text = texts[entry]
            return json.dumps({"status": "ok", "id": contract["id"], "task_status": contract["status"],
                               "entry_count": len(texts), "entry": entry, "offset": offset,
                               "total_chars": len(text), "text": text[offset:offset + limit],
                               "has_more": offset + limit < len(text)}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"status": "error", "error": type(exc).__name__})
