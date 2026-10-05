"""Bundled-plugin acceptance: real discovery, settings, rows and consumers.

Run through the host's canonical test runner. No installed-home changes,
network models, real transports or service restarts are used.
"""
import importlib.util
import json

from pathlib import Path
from types import SimpleNamespace

import pytest

from task_context.store import Store


@pytest.fixture
def installation(tmp_path):
    import agent.context_engine
    host = Path(agent.context_engine.__file__).resolve().parents[1]

    # General plugin activation registers the retained guard/restart read hook;
    # context.engine selects its compressor implementation.
    config = {"context": {"engine": "task-context"}, "plugins": {"enabled": ["task-context"]},
              "task_intents": {"relationship_judge": {"enabled": False}},
              "compression": {"threshold": 0.81, "protect_first_n": 2, "protect_last_n": 9,
                "target_ratio": 0.27, "threshold_tokens": 170000, "min_tail_user_messages": 3,
                "tail_mode": "legacy", "abort_on_summary_failure": True,
                "model_thresholds": {"test-model": 0.84}, "proactive_prune_tokens": 100000,
                "proactive_prune_min_result_chars": 7500, "proactive_prune_min_reclaim_tokens": 2000,
                "micro_compact": True, "micro_compact_every_n_turns": 4}}
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    return host, config


def build(config, home, *, name="task-context", sid="session"):
    from hermes_state import SessionDB
    from agent.agent_init import _parse_compression_config, _build_context_engine, _inject_context_engine_tools
    db = SessionDB(db_path=home / "state.db")
    db.create_session(sid, source="discord")
    agent = SimpleNamespace(session_id=sid, model="test-model", provider="openai", base_url="http://127.0.0.1:9/v1",
        api_key="fixture", api_mode="chat_completions", max_tokens=12000, quiet_mode=True,
        platform="discord", tools=[], valid_tool_names=set(), enabled_toolsets=None,
        _session_db=db, _memory_manager=None, side_agent=False, _compression_threshold_autoraised=None)
    cfg = {**config, "context": {"engine": name}}
    cs = _parse_compression_config(agent, cfg)
    _build_context_engine(agent, cfg, cs, [], 256000, db)
    _inject_context_engine_tools(agent)
    return agent


def test_real_discovery_settings_and_contract_reader(installation, tmp_path):
    _, cfg = installation
    native, plugin = build(cfg, tmp_path, name="compressor", sid="native"), build(cfg, tmp_path)
    assert plugin.context_compressor.name == "task-context"
    from hermes_cli.plugins import get_plugin_manager
    info = next(p for p in get_plugin_manager().list_plugins() if p['name'] == 'task-context')
    assert info['source'] == 'bundled'
    assert not (tmp_path / 'plugins' / 'task-context').exists()
    a, b = native.context_compressor, plugin.context_compressor
    fields = ["context_length", "threshold_tokens", "threshold_percent", "protect_first_n", "protect_last_n",
              "summary_target_ratio", "model_thresholds", "_micro_compact_enabled", "_micro_compact_every_n_turns"]
    for field in fields:
        assert getattr(a, field) == getattr(b, field), field
    from agent.input_provenance import notify_input, event_display_metadata
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource
    from hermes_cli.task_contracts import load_task_contract
    source = SessionSource(platform=Platform.DISCORD, chat_id="channel", thread_id="thread", chat_type="thread", user_id="alice")
    event = MessageEvent(text="  Implement A and B\n保持原文  ", source=source, message_id="m1")
    row = {"role": "user", "content": "decorated", "display_metadata": event_display_metadata(event)}
    notify_input(plugin, row)
    contract = load_task_contract("session", db=plugin._session_db)
    assert contract.raw_primary_text == event.text
    assert Store(tmp_path, read_only=True).contract("session")["id"] == contract.id
    assert any(t["name"] == "task_context_read" for t in plugin.context_compressor.get_tool_schemas())
    native._session_db.close()
    plugin._session_db.close()


@pytest.mark.parametrize('enabled, selected', [
    (False, 'compressor'), (False, 'task-context'), (True, 'compressor'),
])
def test_bundled_plugin_requires_enablement_and_selection(tmp_path, enabled, selected):
    config = {'plugins': {'enabled': ['task-context'] if enabled else []},
              'context': {'engine': selected}}
    (tmp_path / 'config.yaml').write_text(json.dumps(config))
    agent = build(config, tmp_path, name=selected)
    try:
        from hermes_cli.task_contracts import load_task_contract
        from hermes_cli.plugins import get_plugin_manager
        assert agent.context_compressor.name == 'compressor'
        assert load_task_contract('session', db=agent._session_db) is None
        info = next(p for p in get_plugin_manager().list_plugins() if p['name'] == 'task-context')
        assert info['source'] == 'bundled'
        assert info['enabled'] is enabled
        assert not (tmp_path / 'task-context').exists()
    finally:
        agent._session_db.close()


def test_restart_and_status_guard_read_imported_identity(installation, tmp_path):
    _, cfg = installation
    from hermes_state import SessionDB
    from task_context.policy import TaskIntentState, TaskContract
    from hermes_cli.task_contracts import load_task_contract
    from hermes_cli.pre_send_status_guard import active_task_payload_from_task_intent
    from gateway.restart_continuation import _task_contract, hydrate_task
    state = TaskIntentState(id="legacy-id", created_at=1, updated_at=1, status="active", task_contract=TaskContract(raw_primary_text="raw primary", raw_supplements=["keep β"]))
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.set_meta("task_intent:s", state.to_json())
        contract = load_task_contract("s", db=db)
        assert contract.id == "legacy-id"
        payload = active_task_payload_from_task_intent(contract)
        assert payload["raw_primary_text"] == "raw primary"
        snapshot = _task_contract(db, "s")
        assert snapshot["intent_id"] == "legacy-id"
        runner = SimpleNamespace(session_store=SimpleNamespace(_db_for_key=lambda k: db))
        hydrated = hydrate_task(runner, SimpleNamespace(session_id="s", session_key="key"), {"task": snapshot})
        assert hydrated["task"]["supplements"][0]["text"] == "keep β"


def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_real_gateway_channel_routes_into_plugin_without_live_transport(installation, tmp_path, monkeypatch):
    host, cfg = installation
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "u1")
    harness = _module(host / "tests/gateway/restart_continuation_harness.py", "task_context_gateway_harness")
    from hermes_cli import task_contracts
    real_reader = task_contracts.load_task_contract
    import sys, run_agent
    monkeypatch.setitem(sys.modules, "run_agent", run_agent)
    harness.install_fakes(monkeypatch.setattr)
    monkeypatch.setattr(task_contracts, "load_task_contract", real_reader)
    base = harness.ScriptedAgent
    class EngineAgent(base):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            built = build(cfg, tmp_path, sid=self.session_id)
            self.context_compressor = built.context_compressor
            self.platform = "telegram"
        def _persist_user_row(self, message, kwargs):
            from agent.input_provenance import notify_input
            row = {"role": "user", "content": message, "display_metadata": kwargs.get("persist_user_display_metadata") or {}}
            notify_input(self, row)
            super()._persist_user_row(message, kwargs)
    import sys
    monkeypatch.setattr(sys.modules["run_agent"], "AIAgent", EngineAgent)
    runner, adapter, restarts = harness.boot(monkeypatch.setattr)
    harness.ScriptedAgent.script = [{"final": "Recorded."}]
    source = harness.dm_source("group-channel")
    source.chat_type = "group"
    source.thread_id = "thread"
    raw = "  Implement A + B\n後續不能只做 B  "
    await harness.deliver(runner, adapter, raw, "platform-input-1", source)
    snap = harness.session_snapshot(runner, source)
    contract = Store(tmp_path, read_only=True).contract(snap["session_id"])
    assert contract["raw_primary_text"] == raw
    assert adapter.sent and not restarts


@pytest.mark.parametrize("drain", ["pre_api", "post_tool"])
def test_gateway_busy_steer_reaches_canonical_store(installation, tmp_path, drain):
    from agent.input_provenance import notify_input, event_display_metadata
    from agent.interrupt_control import InterruptControlMixin
    from gateway.run import GatewayRunner
    from gateway.config import GatewayConfig
    from gateway.platforms.base import MessageEvent
    from gateway.config import Platform
    from gateway.session import SessionSource
    _, cfg = installation
    cfg["task_intents"]["relationship_judge"]["enabled"] = True
    (tmp_path / "config.yaml").write_text(json.dumps(cfg))
    agent = build(cfg, tmp_path)
    calls = []
    def judge(**kw):
        text = json.loads(kw["messages"][1]["content"])["current_message"]["text"]
        calls.append(text)
        return json.dumps({"relationship": "supplement", "state_effect": "append_contract", "confidence": 1, "evidence_quotes": [text]})
    agent.context_compressor._task_judge_call = judge
    source = SessionSource(platform=Platform.DISCORD, chat_id="channel", thread_id="thread", chat_type="thread", user_id="alice")
    initial = MessageEvent(text="A and B", source=source, message_id="first")
    notify_input(agent, {"role": "user", "content": initial.text, "display_metadata": event_display_metadata(initial)})
    agent.steer = lambda text: InterruptControlMixin.steer(agent, text)
    agent._drain_pending_steer = lambda: InterruptControlMixin._drain_pending_steer(agent)
    agent._active_children = []
    runner = GatewayRunner(GatewayConfig())
    raw = "  Also keep β\nspacing  "
    correction = MessageEvent(text=raw, source=source, message_id="second")
    assert runner._try_agent_verb(agent, "steer", "[decorated] " + raw, "session-key", event=correction)
    rows = [{"role": "user", "content": "A and B"}, {"role": "assistant", "content": "", "tool_calls": []}, {"role": "tool", "content": "tool result", "tool_call_id": "t"}]
    if drain == "pre_api":
        from agent.turn_iteration_prep import _inject_steer_after_newest_tool_result
        _inject_steer_after_newest_tool_result(agent, rows, InterruptControlMixin._drain_pending_steer(agent))
    else:
        from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
        apply_pending_steer_to_tool_results(agent, rows, 1)
    contract = Store(tmp_path, read_only=True).contract("session")
    assert contract["raw_primary_text"] == "A and B"
    assert contract["raw_supplements"] == [raw]
    assert calls == [raw]
    assert rows[-1]["display_metadata"]["input_provenance"][0]["message_id"] == "second"
    notify_input(agent, rows[-1])
    assert calls == [raw]
    agent._session_db.close()



def test_resume_restore_on_actual_wire_builder_keeps_identity_off_wire(installation, tmp_path):
    from copy import deepcopy
    import logging
    from agent.input_provenance import input_record, notify_input
    from agent.turn_context import build_api_messages
    from agent.conversation_loop import _apply_context_engine_selection
    _, cfg = installation
    agent = build(cfg, tmp_path)
    def row(text, identity):
        return {"role": "user", "content": text, "message_uid": identity, "display_metadata": {
            "input_provenance": [input_record(text, source_kind="human", occurrence_id=identity)]}}
    primary, current = row("Implement A and B exactly", "first"), row("continue", "second")
    notify_input(agent, primary)
    notify_input(agent, current)
    # Resume into a transcript whose current input is known but task words were lost.
    transcript = [{"role": "assistant", "content": "old answer"}, current]
    before = deepcopy(transcript)
    agent._current_turn_timestamp = 1
    agent._copy_reasoning_content_for_api = lambda *args: None
    agent._should_sanitize_tool_calls = lambda: False
    agent.ephemeral_system_prompt = ""
    links = {}
    api, system = build_api_messages(agent, transcript, current_turn_user_idx=1,
        ext_prefetch_cache=None, plugin_user_context=None, moa_config=None,
        active_system_prompt="system bytes", selection_metadata=links)
    assert all("display_metadata" not in m and "message_uid" not in m for m in api)
    # Prefill and duplicate content must not make the hook guess by row number/text.
    api.insert(1, {"role": "user", "content": "continue"})
    selected = _apply_context_engine_selection(agent, api, transcript, current,
        logger=logging.getLogger(__name__), selection_metadata=links)
    again = _apply_context_engine_selection(agent, api, transcript, current,
        logger=logging.getLogger(__name__), selection_metadata=links)
    assert selected == again
    assert "Implement A and B exactly" in selected[-1]["content"]
    assert selected[1]["content"] == "continue"
    assert selected[0]["content"] == system == "system bytes"
    assert all("display_metadata" not in m and "message_uid" not in m for m in selected)
    assert transcript == before
    agent._session_db.close()



@pytest.mark.parametrize("accepted", [True, False])
def test_native_codex_redirect_observes_only_acknowledged_input(installation, tmp_path, accepted):
    from agent.interrupt_control import InterruptControlMixin
    from agent.input_provenance import InputText, input_record
    _, cfg = installation
    agent = build(cfg, tmp_path)
    agent.api_mode = "codex_app_server"
    agent._interrupt_requested = False
    agent._pending_redirect = None
    delivered = []
    def request(text):
        delivered.append(str(text))
        return accepted
    agent._codex_session = SimpleNamespace(request_steer=request)
    raw = "  Keep α and β\nexactly  "
    records = [input_record(raw, source_kind="human", occurrence_id="native-input")]
    assert InterruptControlMixin.redirect(agent, InputText(raw, records)) is accepted
    assert delivered == [raw.strip()]
    contract = Store(tmp_path, read_only=True).contract("session")
    if accepted:
        assert contract["raw_primary_text"] == raw
    else:
        assert contract is None
    assert agent._pending_redirect is None
    agent._session_db.close()


@pytest.mark.parametrize("surface", ["tui", "cli", "gateway"])
def test_branch_copy_keeps_constituents_before_later_uid_merge(tmp_path, surface, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "cli")
    from unittest.mock import MagicMock
    from hermes_state import SessionDB
    from task_context.migration import ensure_import
    from test_branch_boundaries import accept
    from conftest import record
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("parent", source="cli")
        store = Store(tmp_path)
        ensure_import(store, "parent", db)
        original = accept(db, store, "Primary", "first")
        if surface == "tui":
            from tui_gateway.methods_session import _persist_branch
            _persist_branch(db, "branch", "parent", "test", [original], source="cli",
                            cwd=str(tmp_path), profile_name="default", model="test")
            sid = "branch"
        elif surface == "gateway":
            from gateway.slash_commands_session import _branch_row
            db.create_session("branch", source="cli", parent_session_id="parent",
                              model_config={"_branched_from": "parent"})
            db.append_messages_batch("branch", [_branch_row(original)])
            sid = "branch"
        else:
            from cli import HermesCLI
            obj = MagicMock()
            obj._agent_running = False
            obj._session_db = db
            obj.session_id = "parent"
            obj.model = "test"
            obj.max_turns = 10
            obj.reasoning_config = {}
            obj.agent = None
            obj.conversation_history = [original]
            HermesCLI._handle_branch_command(obj, "/branch")
            sid = obj.session_id
            assert sid != "parent"
        copied = db.get_messages_as_conversation(sid)[0]
        assert copied["message_uid"] == original["message_uid"]
        assert copied["display_metadata"]["input_provenance"] == original["display_metadata"]["input_provenance"]
        accept(db, store, "Late parent-only supplement", "late")
        store.link("parent", [record("Late parent-only supplement", "late")], original["message_uid"])
        ensure_import(store, sid, db)
        assert store.contract(sid)["raw_primary_text"] == "Primary"
        assert store.contract(sid)["raw_supplements"] == []
        assert store.contract("parent")["raw_supplements"] == ["Late parent-only supplement"]
