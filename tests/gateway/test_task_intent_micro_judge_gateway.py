from __future__ import annotations

from collections import OrderedDict
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._task_intent_judge_cache = OrderedDict()
    runner._task_intent_judge_signature = ""
    runner._task_intent_judge_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner._last_resolved_model = {}
    runner._fallback_model = None
    runner._provider_routing = {}
    runner._resolve_session_agent_runtime = MagicMock(
        return_value=(
            "test/main-model",
            {
                "provider": "openai",
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
                "api_mode": "chat_completions",
            },
        )
    )
    return runner


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import task_intents

    task_intents._DB_CACHE.clear()
    yield home
    task_intents._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_gateway_micro_judge_supplies_structured_decision_to_task_manager(monkeypatch, hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    runner = _make_runner()
    mgr = TaskIntentManager("sid-gateway-micro")
    mgr.record_direct_message("Build a dashboard")

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"task_intents": {"relationship_judge": {"enabled": True, "timeout_seconds": 1.0}}},
    )
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return '{"relationship":"supplement","state_effect":"append_contract","confidence":0.74,"evidence_quotes":["CSV"]}'

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)

    decision = await runner._judge_direct_task_relationship(
        manager=mgr,
        raw_text="also export CSV",
        message_id="discord-msg-2",
        session_key="discord:u1:c1:t1",
    )
    state = mgr.record_direct_message(
        "also export CSV",
        source_kind="direct_user",
        message_id="discord-msg-2",
        relationship_decision=decision,
    )

    assert decision is not None
    assert decision.relationship == "supplement"
    assert state.task_contract.raw_primary_text == "Build a dashboard"
    assert state.task_contract.raw_supplements == ["also export CSV"]
    assert state.raw_messages[-1].message_id == "discord-msg-2"
    assert calls[0]["task"] == "task_intent"
    assert calls[0]["model"] == "test/main-model"
    assert calls[0]["tools"] is None
    assert calls[0]["temperature"] == 0
    assert calls[0]["main_runtime"]["provider"] == "openai"


@pytest.mark.asyncio
async def test_gateway_micro_judge_cache_avoids_second_llm_call(monkeypatch, hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    runner = _make_runner()
    mgr = TaskIntentManager("sid-gateway-cache")
    mgr.record_direct_message("Build a dashboard")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"task_intents": {"relationship_judge": {"enabled": True}}},
    )
    calls = []
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **kwargs: calls.append(kwargs) or '{"relationship":"related_question","state_effect":"related_only","confidence":0.7}',
    )

    one = await runner._judge_direct_task_relationship(
        manager=mgr,
        raw_text="what is next?",
        message_id="discord-msg-2",
        session_key="discord:u1:c1:t1",
    )
    two = await runner._judge_direct_task_relationship(
        manager=mgr,
        raw_text="what is next?",
        message_id="discord-msg-2",
        session_key="discord:u1:c1:t1",
    )

    assert one is not None and two is not None
    assert one.raw_payload["cache"] == "miss"
    assert two.raw_payload["cache"] == "hit"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_gateway_micro_judge_failure_records_conservative_fallback(monkeypatch, hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    runner = _make_runner()
    mgr = TaskIntentManager("sid-gateway-fallback")
    mgr.record_direct_message("Build a dashboard")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"task_intents": {"relationship_judge": {"enabled": True}}},
    )

    def failing_call_llm(**_):
        raise RuntimeError("provider secret should not persist")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", failing_call_llm)

    decision = await runner._judge_direct_task_relationship(
        manager=mgr,
        raw_text="new thing maybe",
        message_id="discord-msg-2",
        session_key="discord:u1:c1:t1",
    )
    state = mgr.record_direct_message(
        "new thing maybe",
        source_kind="direct_user",
        message_id="discord-msg-2",
        relationship_decision=decision,
    )

    assert decision is not None
    assert decision.relationship == "unclear"
    assert decision.state_effect == "no_change"
    assert decision.fallback_reason == "relationship judge failed"
    assert state.task_contract.raw_primary_text == "Build a dashboard"
    assert state.task_contract.raw_supplements == []
    assert "provider secret" not in state.raw_messages[-1].judge_result["fallback_reason"]


@pytest.mark.asyncio
async def test_gateway_micro_judge_disabled_or_no_active_task_skips_llm(monkeypatch, hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    runner = _make_runner()
    mgr = TaskIntentManager("sid-gateway-disabled")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"task_intents": {"relationship_judge": {"enabled": False}}},
    )
    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **_: (_ for _ in ()).throw(AssertionError("should not call")))

    assert await runner._judge_direct_task_relationship(
        manager=mgr,
        raw_text="first task",
        message_id="discord-msg-1",
        session_key="discord:u1:c1:t1",
    ) is None

    mgr.record_direct_message("Build a dashboard")
    assert await runner._judge_direct_task_relationship(
        manager=mgr,
        raw_text="followup",
        message_id="discord-msg-2",
        session_key="discord:u1:c1:t1",
    ) is None


@pytest.mark.asyncio
async def test_gateway_micro_judge_absent_or_failed_config_is_opt_in_disabled(monkeypatch, hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    runner = _make_runner()
    mgr = TaskIntentManager("sid-gateway-no-config")
    mgr.record_direct_message("Build a dashboard")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **_: (_ for _ in ()).throw(AssertionError("should not call")))

    assert await runner._judge_direct_task_relationship(
        manager=mgr,
        raw_text="followup",
        message_id="discord-msg-2",
        session_key="discord:u1:c1:t1",
    ) is None

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: (_ for _ in ()).throw(RuntimeError("config unavailable")))
    assert await runner._judge_direct_task_relationship(
        manager=mgr,
        raw_text="followup again",
        message_id="discord-msg-3",
        session_key="discord:u1:c1:t1",
    ) is None

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"task_intents": {"relationship_judge": False}})
    assert await runner._judge_direct_task_relationship(
        manager=mgr,
        raw_text="explicitly disabled followup",
        message_id="discord-msg-4",
        session_key="discord:u1:c1:t1",
    ) is None


@pytest.mark.asyncio
async def test_discord_handle_message_runs_micro_judge_before_agent(monkeypatch, hermes_home):
    from hermes_cli.task_intents import TaskIntentManager
    from gateway.run import GatewayRunner

    session_id = "sid-discord-real-flow"
    session_key = "discord:user-1:chat-1:thread-1"
    source = SessionSource(
        platform=Platform.DISCORD,
        user_id="user-1",
        chat_id="chat-1",
        thread_id="thread-1",
        user_name="tester",
        chat_type="dm",
    )
    event = MessageEvent(
        text="also export CSV",
        message_id="discord-msg-2",
        source=source,
    )

    # Existing active task from a previous real user turn in this same gateway session.
    TaskIntentManager(session_id).record_direct_message("Build a dashboard")

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True)})
    runner.adapters = {Platform.DISCORD: SimpleNamespace(send=AsyncMock(), stop_typing=AsyncMock())}
    runner.session_store = MagicMock()
    session_entry = SimpleNamespace(
        session_id=session_id,
        session_key=session_key,
        created_at=1,
        updated_at=2,
        last_prompt_tokens=0,
        was_auto_reset=False,
        is_fresh_reset=False,
    )
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = [{"role": "user", "content": "Build a dashboard"}]
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner.session_store.clear_resume_pending = MagicMock()
    runner._session_db = None
    runner._session_model_overrides = {}
    runner._last_resolved_model = {}
    runner._fallback_model = None
    runner._provider_routing = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._update_prompt_pending = {}
    runner._pending_native_image_paths_by_session = {}
    runner._task_intent_judge_cache = OrderedDict()
    runner._task_intent_judge_signature = ""
    runner._task_intent_judge_cache_lock = threading.Lock()
    runner._show_reasoning = False
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner.pairing_store = MagicMock()
    runner._is_user_authorized = MagicMock(return_value=True)
    runner._session_key_for_source = MagicMock(return_value=session_key)
    runner._recover_telegram_topic_thread_id = MagicMock(return_value=None)
    runner._is_telegram_topic_lane = MagicMock(return_value=False)
    runner._cache_session_source = MagicMock()
    runner._set_session_env = MagicMock(return_value=[])
    runner._clear_session_env = MagicMock()
    runner._is_telegram_topic_root_lobby = MagicMock(return_value=False)
    runner._begin_session_run_generation = MagicMock(return_value=1)
    runner._release_running_agent_state = MagicMock()
    runner._bind_adapter_run_generation = MagicMock()
    runner._is_session_run_current = MagicMock(return_value=True)
    runner._reply_anchor_for_event = MagicMock(return_value="discord-msg-2")
    runner._resolve_session_agent_runtime = MagicMock(return_value=("test/main-model", {"provider": "openai", "api_key": "k"}))
    runner._should_send_voice_reply = MagicMock(return_value=False)
    runner._post_turn_goal_continuation = AsyncMock()
    runner._thread_metadata_for_source = MagicMock(return_value={})
    runner._get_guild_id = MagicMock(return_value=None)
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
            "history_offset": 1,
            "last_prompt_tokens": 0,
            "already_sent": False,
        }
    )

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_, **__: [])
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"task_intents": {"relationship_judge": {"enabled": True}}})
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return '{"relationship":"supplement","state_effect":"append_contract","confidence":0.8,"evidence_quotes":["CSV"]}'

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)

    response = await runner._handle_message(event)

    state = TaskIntentManager(session_id).state
    assert response == "done"
    assert state.task_contract.raw_primary_text == "Build a dashboard"
    assert state.task_contract.raw_supplements == ["also export CSV"]
    assert state.raw_messages[-1].message_id == "discord-msg-2"
    assert state.raw_messages[-1].relationship_to_active_task == "supplement"
    assert calls[0]["task"] == "task_intent"
    assert calls[0]["model"] == "test/main-model"
    assert calls[0]["tools"] is None
    runner._run_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_micro_judge_timeout_none_keeps_manager_unclear_fallback(monkeypatch, hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    runner = _make_runner()
    mgr = TaskIntentManager("sid-gateway-timeout-fallback")
    mgr.record_direct_message("Build a dashboard")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"task_intents": {"relationship_judge": {"enabled": True, "timeout_seconds": 0.05}}},
    )

    def slow_call_llm(**_):
        import time
        time.sleep(0.7)
        return '{"relationship":"supplement","state_effect":"append_contract","confidence":0.9}'

    monkeypatch.setattr("agent.auxiliary_client.call_llm", slow_call_llm)
    decision = await runner._judge_direct_task_relationship(
        manager=mgr,
        raw_text="also export CSV",
        message_id="discord-msg-2",
        session_key="discord:u1:c1:t1",
    )
    state = mgr.record_direct_message(
        "also export CSV",
        source_kind="direct_user",
        message_id="discord-msg-2",
        relationship_decision=decision,
    )

    assert decision is None
    assert state.task_contract.raw_primary_text == "Build a dashboard"
    assert state.task_contract.raw_supplements == []
    assert state.raw_messages[-1].relationship_to_active_task == "unclear"
    await asyncio.sleep(0.3)
    assert list(runner._task_intent_judge_cache.values()) == []


def _make_realistic_discord_runner_for_task_intent(session_id: str, session_key: str):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True)})
    runner.adapters = {Platform.DISCORD: SimpleNamespace(send=AsyncMock(), stop_typing=AsyncMock())}
    runner.session_store = MagicMock()
    session_entry = SimpleNamespace(
        session_id=session_id,
        session_key=session_key,
        created_at=1,
        updated_at=2,
        last_prompt_tokens=0,
        was_auto_reset=False,
        is_fresh_reset=False,
    )
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = [{"role": "user", "content": "Build a dashboard"}]
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner.session_store.clear_resume_pending = MagicMock()
    runner._session_db = None
    runner._session_model_overrides = {}
    runner._last_resolved_model = {}
    runner._fallback_model = None
    runner._provider_routing = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._update_prompt_pending = {}
    runner._pending_native_image_paths_by_session = {}
    runner._task_intent_judge_cache = OrderedDict()
    runner._task_intent_judge_signature = ""
    runner._task_intent_judge_cache_lock = threading.Lock()
    runner._show_reasoning = False
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner.pairing_store = MagicMock()
    runner._is_user_authorized = MagicMock(return_value=True)
    runner._session_key_for_source = MagicMock(return_value=session_key)
    runner._recover_telegram_topic_thread_id = MagicMock(return_value=None)
    runner._is_telegram_topic_lane = MagicMock(return_value=False)
    runner._cache_session_source = MagicMock()
    runner._set_session_env = MagicMock(return_value=[])
    runner._clear_session_env = MagicMock()
    runner._is_telegram_topic_root_lobby = MagicMock(return_value=False)
    runner._begin_session_run_generation = MagicMock(return_value=1)
    runner._release_running_agent_state = MagicMock()
    runner._bind_adapter_run_generation = MagicMock()
    runner._is_session_run_current = MagicMock(return_value=True)
    runner._reply_anchor_for_event = MagicMock(return_value="discord-msg")
    runner._resolve_session_agent_runtime = MagicMock(return_value=("test/main-model", {"provider": "openai", "api_key": "k"}))
    runner._should_send_voice_reply = MagicMock(return_value=False)
    runner._post_turn_goal_continuation = AsyncMock()
    runner._thread_metadata_for_source = MagicMock(return_value={})
    runner._get_guild_id = MagicMock(return_value=None)
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "done",
            "messages": [],
            "api_calls": 1,
            "history_offset": 1,
            "last_prompt_tokens": 0,
            "already_sent": False,
        }
    )
    return runner


def _discord_dm_event(text: str, *, message_id: str = "discord-msg", internal: bool = False) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_id=message_id,
        internal=internal,
        source=SessionSource(
            platform=Platform.DISCORD,
            user_id="user-1",
            chat_id="chat-1",
            thread_id="thread-1",
            user_name="tester",
            chat_type="dm",
        ),
    )


@pytest.mark.asyncio
async def test_discord_handle_message_timeout_preserves_active_contract_and_still_runs_agent(monkeypatch, hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    session_id = "sid-discord-timeout-flow"
    runner = _make_realistic_discord_runner_for_task_intent(session_id, "discord:user-1:chat-1:thread-1")
    TaskIntentManager(session_id).record_direct_message("Build a dashboard")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_, **__: [])
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"task_intents": {"relationship_judge": {"enabled": True, "timeout_seconds": 0.05}}})
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})

    def slow_call_llm(**_):
        import time
        time.sleep(0.7)
        return '{"relationship":"supplement","state_effect":"append_contract","confidence":0.9}'

    monkeypatch.setattr("agent.auxiliary_client.call_llm", slow_call_llm)

    response = await runner._handle_message(_discord_dm_event("also export CSV", message_id="discord-timeout"))
    state = TaskIntentManager(session_id).state

    assert response == "done"
    assert state.task_contract.raw_primary_text == "Build a dashboard"
    assert state.task_contract.raw_supplements == []
    assert state.raw_messages[-1].relationship_to_active_task == "unclear"
    runner._run_agent.assert_awaited_once()
    await asyncio.sleep(0.3)
    assert list(runner._task_intent_judge_cache.values()) == []


@pytest.mark.asyncio
async def test_internal_discord_event_does_not_run_micro_judge_or_mutate_task_intent(monkeypatch, hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    session_id = "sid-discord-internal-boundary"
    runner = _make_realistic_discord_runner_for_task_intent(session_id, "discord:user-1:chat-1:thread-1")
    TaskIntentManager(session_id).record_direct_message("Build a dashboard")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_, **__: [])
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"task_intents": {"relationship_judge": {"enabled": True}}})
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **_: (_ for _ in ()).throw(AssertionError("internal event must not call task_intent judge")),
    )

    response = await runner._handle_message(
        _discord_dm_event("also export CSV", message_id="discord-internal", internal=True)
    )
    state = TaskIntentManager(session_id).state

    assert response == "done"
    assert state.task_contract.raw_primary_text == "Build a dashboard"
    assert state.task_contract.raw_supplements == []
    assert [m.raw_text for m in state.raw_messages] == ["Build a dashboard"]
    runner._run_agent.assert_awaited_once()
