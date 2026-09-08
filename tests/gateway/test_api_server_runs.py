"""Tests for /v1/runs endpoints: start, status, events, steer, and stop.

Covers:
- POST /v1/runs — start a run (202)
- GET /v1/runs/{run_id} — poll run status
- GET /v1/runs/{run_id}/events — SSE event stream
- POST /v1/runs/{run_id}/steer — inject guidance into a running agent
- POST /v1/runs/{run_id}/stop — interrupt a running agent
- Auth, error handling, and cleanup
"""

import asyncio
import json
from pathlib import PurePosixPath
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    _approval_event_choices,
    cors_middleware,
    security_headers_middleware,
)
from tools import approval as approval_mod
from agent.delegation_policy import AccessMode, DelegationSessionPolicy, ExecutionProfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("smart_denied", "allow_permanent", "expected"),
    [
        (False, True, ["once", "session", "always", "deny"]),
        (False, False, ["once", "session", "deny"]),
        (True, True, ["once", "deny"]),
        (True, False, ["once", "deny"]),
    ],
)
def test_approval_event_choices_follow_backend_capabilities(
    smart_denied, allow_permanent, expected
):
    assert _approval_event_choices(
        smart_denied=smart_denied,
        allow_permanent=allow_permanent,
    ) == expected


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    """Create an adapter with optional API key."""
    extra = {}
    if api_key:
        extra["key"] = api_key
    config = PlatformConfig(enabled=True, extra=extra)
    adapter = APIServerAdapter(config)
    return adapter


def _create_runs_app(adapter: APIServerAdapter) -> web.Application:
    """Create an aiohttp app with /v1/runs routes registered."""
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
    app.router.add_post("/v1/runs/{run_id}/approval", adapter._handle_run_approval)
    app.router.add_post("/v1/runs/{run_id}/steer", adapter._handle_steer_run)
    app.router.add_post("/v1/runs/{run_id}/stop", adapter._handle_stop_run)
    return app


def _make_slow_agent(**kwargs):
    """Create a mock agent that blocks in run_conversation until interrupted.

    Returns (mock_agent, agent_ready_event, interrupt_event) where
    agent_ready_event is set once run_conversation starts, and
    interrupt_event is set when interrupt() is called.
    """
    ready = threading.Event()
    interrupted = threading.Event()

    mock_agent = MagicMock()

    def _do_interrupt(message=None):
        interrupted.set()

    mock_agent.interrupt = MagicMock(side_effect=_do_interrupt)

    def _slow_run(user_message=None, conversation_history=None, task_id=None):
        ready.set()
        # Block until interrupt() is called
        interrupted.wait(timeout=10)
        return {"final_response": "interrupted"}

    mock_agent.run_conversation.side_effect = _slow_run
    mock_agent.session_prompt_tokens = 0
    mock_agent.session_completion_tokens = 0
    mock_agent.session_total_tokens = 0

    return mock_agent, ready, interrupted


@pytest.fixture
def adapter():
    return _make_adapter()


@pytest.fixture
def auth_adapter():
    return _make_adapter(api_key="sk-secret")


def test_run_event_callback_preserves_complete_tool_items(adapter):
    queue = asyncio.Queue()
    adapter._run_streams["run-tool-detail"] = queue
    loop = MagicMock()
    loop.call_soon_threadsafe.side_effect = lambda callback, *args: callback(*args)
    callback = adapter._make_run_event_callback("run-tool-detail", loop)

    callback(
        "tool.started",
        "web_search",
        "archive interaction patterns",
        {"query": "archive interaction patterns", "limit": 5},
    )
    callback(
        "tool.completed",
        "web_search",
        None,
        None,
        duration=1.234,
        is_error=False,
        result='{"success":true,"count":5}',
    )

    started = queue.get_nowait()
    completed = queue.get_nowait()
    assert started["args"] == {
        "query": "archive interaction patterns",
        "limit": 5,
    }
    assert completed["result"] == '{"success":true,"count":5}'


# ---------------------------------------------------------------------------
# POST /v1/runs — start a run
# ---------------------------------------------------------------------------


class TestStartRun:
    @pytest.mark.asyncio
    async def test_start_returns_202(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 10
                mock_agent.session_completion_tokens = 5
                mock_agent.session_total_tokens = 15
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                assert data["status"] == "started"
                assert data["run_id"].startswith("run_")

                status_resp = await cli.get(f"/v1/runs/{data['run_id']}")
                assert status_resp.status == 200
                status = await status_resp.json()
                assert status["run_id"] == data["run_id"]
                assert status["status"] in {"queued", "running", "completed"}
                assert status["object"] == "hermes.run"

    @pytest.mark.asyncio
    async def test_start_binds_chat_id_for_delegation_wake_target(self, adapter):
        """/v1/runs must bind the raw session id as the api_server chat_id
        (like every other agent-entry route does via _run_agent): the async
        delegation dispatch reads HERMES_SESSION_CHAT_ID to pick its wake
        self-post target, and an empty binding forces background delegations
        on this route back to synchronous execution."""
    @pytest.mark.parametrize(
        ("allowed_tools", "expected_tool_names"),
        [
            (
                {"delegate_task", "skills_list", "skill_view"},
                {"terminal", "delegate_task", "skills_list", "skill_view"},
            ),
            (set(), {"terminal"}),
        ],
    )
    @pytest.mark.asyncio
    async def test_protected_start_uses_root_attempt_as_task_id(
        self,
        adapter,
        tmp_path,
        allowed_tools,
        expected_tool_names,
    ):
        repository = tmp_path / "repository"
        carveout = repository / "readonly"
        carveout.mkdir(parents=True)
        profile = ExecutionProfile(
            name="filesystem-isolated",
            backend="docker",
            image="example@sha256:abc",
            default_workdir="/workspace",
            allowed_toolsets={"terminal", "file"},
            allowed_tools=allowed_tools,
        )
        base_policy = DelegationSessionPolicy(
            profile_required=True,
            allow_profile_none=False,
            allowed_profiles={profile.name},
            profile_snapshots={profile.name: profile},
            visible_objects=(),
            protected_prefixes=(),
        )
        root_registry = MagicMock()
        root_registry.reserve.return_value.attempt_id = "runs-root-attempt"
        root_registry.cleanup.return_value = ()
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "done"}
        mock_agent.session_prompt_tokens = 0
        mock_agent.session_completion_tokens = 0
        mock_agent.session_total_tokens = 0
        mock_agent.tools = [
            {"function": {"name": name}}
            for name in (
                "terminal",
                "memory",
                "delegate_task",
                "skills_list",
                "skill_view",
                "skill_manage",
            )
        ]
        mock_agent.valid_tool_names = {
            item["function"]["name"] for item in mock_agent.tools
        }

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with (
                patch(
                    "agent.agent_init._admit_standard_delegation_policy",
                    return_value=base_policy,
                ),
                patch(
                    "tools.delegation_scope.attempt_scope_registry",
                    root_registry,
                ),
                patch(
                    "tools.skills_tool._find_all_skills",
                    return_value=[{"name": "selected"}],
                ),
                patch(
                    "tools.skills_tool.skill_view",
                    return_value=json.dumps({"success": True}),
                ),
                patch(
                    "tools.delegation_scope.configure_protected_attempt_environment"
                ) as configure_environment,
                patch(
                    "tools.terminal_tool._get_env_config",
                    return_value={"docker_network": False},
                ),
                patch.object(adapter, "_create_agent", return_value=mock_agent) as create,
            ):
                response = await cli.post(
                    "/v1/runs",
                    json={
                        "input": "hello",
                        "instructions": "Follow the operator workflow.  \n",
                        "session_id": "client-session",
                        "execution": {
                            "profile": profile.name,
                            "workdir": str(repository),
                            "reveal": [
                                {"path": str(repository), "mode": "rw"},
                                {"path": str(carveout), "mode": "ro"},
                            ],
                            "skills": ["selected"],
                        },
                    },
                )
                assert response.status == 202
                run_id = (await response.json())["run_id"]
                for _ in range(20):
                    if run_id not in adapter._active_run_tasks:
                        break
                    await asyncio.sleep(0.05)

        create.assert_called_once()
        protected_prompt = create.call_args.kwargs["ephemeral_system_prompt"]
        assert protected_prompt.startswith(
            "Follow the operator workflow.  \n\n\n## Execution filesystem"
        )
        assert f'Working directory: "{repository}"' in protected_prompt
        assert f'"{repository}" — directory, read-write' in protected_prompt
        assert f'"{carveout}" — directory, read-only' in protected_prompt
        assert "most-specific listed mode applies" in protected_prompt
        assert "Other host paths are not available in this attempt." in protected_prompt
        assert create.call_args.kwargs["delegation_policy"].visible_objects
        root_scope = root_registry.reserve.call_args.args[0]
        assert [(item.visible_path, item.mode) for item in root_scope.visible_objects] == [
            (PurePosixPath(str(repository)), AccessMode.RW),
            (PurePosixPath(str(carveout)), AccessMode.RO),
        ]
        assert root_scope.profile.network == "none"
        assert root_registry.reserve.call_args.args[0].skill_names == frozenset(
            {"selected"}
        )
        assert mock_agent.skill_scope_task_id == "runs-root-attempt"
        mock_agent.run_conversation.assert_called_once()
        assert mock_agent.run_conversation.call_args.kwargs["task_id"] == "runs-root-attempt"
        root_registry.prepare_idmapped_reveals.assert_called_once_with("runs-root-attempt")
        configure_environment.assert_called_once_with("runs-root-attempt")
        root_registry.activate.assert_called_once()
        root_registry.cleanup.assert_called_once_with("runs-root-attempt")
        assert {
            item["function"]["name"] for item in mock_agent.tools
        } == expected_tool_names

    @pytest.mark.asyncio
    async def test_protected_start_admits_exact_readonly_file_reveal(
        self,
        adapter,
        tmp_path,
    ):
        readme = tmp_path / "README.md"
        readme.write_text("project context", encoding="utf-8")
        profile = ExecutionProfile(
            name="filesystem-isolated",
            backend="docker",
            image="example@sha256:abc",
            default_workdir="/workspace",
            allowed_toolsets={"terminal", "file"},
            runtime_identity=(10001, 10001),
        )
        base_policy = DelegationSessionPolicy(
            profile_required=True,
            allow_profile_none=False,
            allowed_profiles={profile.name},
            profile_snapshots={profile.name: profile},
            visible_objects=(),
            protected_prefixes=(),
        )
        root_registry = MagicMock()
        root_registry.reserve.return_value.attempt_id = "runs-file-attempt"
        root_registry.cleanup.return_value = ()
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "done"}
        mock_agent.session_prompt_tokens = 0
        mock_agent.session_completion_tokens = 0
        mock_agent.session_total_tokens = 0
        mock_agent.tools = [{"function": {"name": "terminal"}}]
        mock_agent.valid_tool_names = {"terminal"}

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with (
                patch(
                    "agent.agent_init._admit_standard_delegation_policy",
                    return_value=base_policy,
                ),
                patch(
                    "tools.delegation_scope.attempt_scope_registry",
                    root_registry,
                ),
                patch(
                    "tools.delegation_scope.configure_protected_attempt_environment"
                ),
                patch(
                    "tools.terminal_tool._get_env_config",
                    return_value={"docker_network": False},
                ),
                patch.object(adapter, "_create_agent", return_value=mock_agent) as create,
            ):
                response = await cli.post(
                    "/v1/runs",
                    json={
                        "input": "read context",
                        "execution": {
                            "profile": profile.name,
                            "workdir": "/workspace",
                            "reveal": [{"path": str(readme), "mode": "ro"}],
                        },
                    },
                )
                assert response.status == 202
                run_id = (await response.json())["run_id"]
                for _ in range(20):
                    if run_id not in adapter._active_run_tasks:
                        break
                    await asyncio.sleep(0.05)

        prompt = create.call_args.kwargs["ephemeral_system_prompt"]
        assert f'"{readme}" — file, read-only' in prompt
        scope = root_registry.reserve.call_args.args[0]
        assert scope.workdir == PurePosixPath("/workspace")
        assert len(scope.visible_objects) == 1
        assert scope.visible_objects[0].visible_path == PurePosixPath(str(readme))
        assert scope.visible_objects[0].object_type == "file"
        assert scope.visible_objects[0].mode is AccessMode.RO
        root_registry.prepare_idmapped_reveals.assert_called_once_with(
            "runs-file-attempt"
        )

    @pytest.mark.asyncio
    async def test_ordinary_start_does_not_add_execution_filesystem_context(self, adapter):
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "done"}
        mock_agent.session_prompt_tokens = 0
        mock_agent.session_completion_tokens = 0
        mock_agent.session_total_tokens = 0

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent", return_value=mock_agent) as create:
                response = await cli.post(
                    "/v1/runs",
                    json={"input": "hello", "instructions": "Keep this exact"},
                )
                assert response.status == 202
                run_id = (await response.json())["run_id"]
                for _ in range(20):
                    if run_id not in adapter._active_run_tasks:
                        break
                    await asyncio.sleep(0.05)

        assert create.call_args.kwargs["ephemeral_system_prompt"] == "Keep this exact"

    @pytest.mark.asyncio
    async def test_explicit_null_execution_fails_closed_without_allocating_run(
        self,
        adapter,
    ):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            response = await cli.post(
                "/v1/runs",
                json={"input": "hello", "execution": None},
            )
            body = await response.json()

        assert response.status == 400
        assert body["error"]["code"] == "invalid_execution"
        assert adapter._run_statuses == {}
        assert adapter._run_streams == {}

    @pytest.mark.asyncio
    async def test_start_invalid_json_returns_400(self, adapter):
        app = _create_runs_app(adapter)
        captured = {}

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()

                def _capture_run(user_message=None, conversation_history=None, task_id=None):
                    from tools.async_delegation import _current_origin_session_id

                    captured["origin_session_id"] = _current_origin_session_id()
                    return {"final_response": "done"}

                mock_agent.run_conversation.side_effect = _capture_run
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hello", "session_id": "runs-raw-sid"},
                )
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                for _ in range(40):
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    status = await status_resp.json()
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

        assert captured.get("origin_session_id") == "runs-raw-sid", (
            "runs route must bind chat_id so delegation dispatch sees a wake target"
        )


    @pytest.mark.asyncio
    async def test_start_rejects_conflicting_route_and_request_provider(self):
        adapter = APIServerAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "model_routes": {
                        "alias": {
                            "model": "route/model",
                            "provider": "openrouter",
                        }
                    }
                },
            )
        )
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                resp = await cli.post(
                    "/v1/runs",
                    json={
                        "input": "hello",
                        "model": "alias",
                        "provider": "minimax",
                    },
                )
                data = await resp.json()

        assert resp.status == 400
        assert "provider" in data["error"]["message"].lower()
        assert adapter._run_streams == {}
        assert adapter._run_statuses == {}
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_passes_request_model_provider_options_to_create_agent(self, adapter):
        app = _create_runs_app(adapter)
        model_options = {"reasoning_effort": "medium", "service_tier": "priority"}
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={
                        "input": "hello",
                        "model": "MiniMax-M3",
                        "provider": "minimax",
                        "model_options": model_options,
                    },
                )
                assert resp.status == 202
                for _ in range(20):
                    if mock_create.call_args is not None:
                        break
                    await asyncio.sleep(0.05)

        kwargs = mock_create.call_args.kwargs
        assert kwargs["requested_model"] == "MiniMax-M3"
        assert kwargs["requested_provider"] == "minimax"
        assert kwargs["model_options"] == model_options


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id} — poll run status
# ---------------------------------------------------------------------------


class TestRunStatus:

    @pytest.mark.asyncio
    async def test_status_reflects_explicit_session_id(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hello", "session_id": "space-session"},
                )
                data = await resp.json()
                run_id = data["run_id"]

                for _ in range(20):
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    status = await status_resp.json()
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

                mock_agent.run_conversation.assert_called_once()
                assert mock_agent.run_conversation.call_args.kwargs["task_id"] == "space-session"
                assert status["session_id"] == "space-session"


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id}/events — SSE event stream
# ---------------------------------------------------------------------------


class TestRunEvents:
    @pytest.mark.asyncio
    async def test_events_stream_returns_completed(self, adapter):
        """Events stream should receive run.completed when agent finishes."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "Hello!"}
                mock_agent.session_prompt_tokens = 10
                mock_agent.session_completion_tokens = 5
                mock_agent.session_total_tokens = 15
                mock_create.return_value = mock_agent

                # Start run
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                # Subscribe to events
                events_resp = await cli.get(f"/v1/runs/{run_id}/events")
                assert events_resp.status == 200
                body = await events_resp.text()

                # Should contain run.completed
                assert "run.completed" in body
                assert "Hello!" in body


    @pytest.mark.asyncio
    async def test_approval_resolve_all_is_scoped_to_target_run(self, auth_adapter):
        """Same client session_id must not let one run approve another run's queue."""
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(auth_adapter, "_create_agent") as mock_create:
                victim_agent, victim_ready, victim_interrupted = _make_slow_agent()
                attacker_agent, attacker_ready, attacker_interrupted = _make_slow_agent()
                mock_create.side_effect = [victim_agent, attacker_agent]

                victim_resp = await cli.post(
                    "/v1/runs",
                    json={"input": "victim", "session_id": "shared-project"},
                    headers={"Authorization": "Bearer sk-secret"},
                )
                attacker_resp = await cli.post(
                    "/v1/runs",
                    json={"input": "attacker", "session_id": "shared-project"},
                    headers={"Authorization": "Bearer sk-secret"},
                )
                assert victim_resp.status == 202
                assert attacker_resp.status == 202
                victim_run = (await victim_resp.json())["run_id"]
                attacker_run = (await attacker_resp.json())["run_id"]

                victim_ready.wait(timeout=3.0)
                attacker_ready.wait(timeout=3.0)
                assert auth_adapter._run_approval_sessions[victim_run] == victim_run
                assert auth_adapter._run_approval_sessions[attacker_run] == attacker_run
                assert auth_adapter._run_approval_sessions[victim_run] != auth_adapter._run_approval_sessions[attacker_run]

                victim_entry = approval_mod._ApprovalEntry({
                    "command": "bash -c victim-danger",
                    "description": "victim approval",
                    "pattern_keys": ["shell-c"],
                })
                attacker_entry = approval_mod._ApprovalEntry({
                    "command": "bash -c attacker-danger",
                    "description": "attacker approval",
                    "pattern_keys": ["shell-c"],
                })
                with approval_mod._lock:
                    approval_mod._gateway_queues[victim_run] = [victim_entry]
                    approval_mod._gateway_queues[attacker_run] = [attacker_entry]

                approval_resp = await cli.post(
                    f"/v1/runs/{attacker_run}/approval",
                    json={"choice": "always", "resolve_all": True},
                    headers={"Authorization": "Bearer sk-secret"},
                )
                approval_data = await approval_resp.json()

                assert approval_resp.status == 200
                assert approval_data["resolved"] == 1
                assert attacker_entry.result == "always"
                assert attacker_entry.event.is_set()
                assert victim_entry.result is None
                assert not victim_entry.event.is_set()
                with approval_mod._lock:
                    assert approval_mod._gateway_queues[victim_run] == [victim_entry]
                    assert victim_run in approval_mod._gateway_queues
                    assert attacker_run not in approval_mod._gateway_queues

                # Clean up the synthetic pending victim approval and unblock the
                # slow test agents so their background run tasks can finish.
                with approval_mod._lock:
                    approval_mod._gateway_queues.pop(victim_run, None)
                victim_interrupted.set()
                attacker_interrupted.set()


# ---------------------------------------------------------------------------
# POST /v1/runs/{run_id}/steer — steer a running agent
# ---------------------------------------------------------------------------


class TestSteerRun:
    @pytest.mark.asyncio
    async def test_active_run_stream_can_reconnect_during_subscriber_handoff(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, interrupted = _make_slow_agent()
                mock_create.return_value = mock_agent

                started = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await started.json())["run_id"]
                assert agent_ready.wait(timeout=3.0)
                stream_delta = mock_create.call_args.kwargs["stream_delta_callback"]

                first = await cli.get(f"/v1/runs/{run_id}/events")
                assert first.status == 200

                # Connect the replacement before the stale client has finalized.
                # The server must transfer sole ownership before reading again.
                second = await cli.get(f"/v1/runs/{run_id}/events")
                assert second.status == 200
                assert run_id in adapter._run_stream_subscribers

                # Emit only after the replacement response is established, so
                # this cannot pass by consuming a previously buffered event.
                stream_delta("after reconnect")
                stop = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop.status == 200
                body = await asyncio.wait_for(second.text(), timeout=3.0)
                assert "after reconnect" in body
                assert "run.cancelled" in body
                assert interrupted.is_set()
                first.close()

    @pytest.mark.asyncio

    async def test_subscriber_handoff_restores_event_interrupted_during_write(self, adapter):
        run_id = "run_write_handoff"
        queue = asyncio.Queue()
        await queue.put({"event": "message.delta", "run_id": run_id, "delta": "preserved"})
        adapter._run_streams[run_id] = queue
        adapter._run_streams_created[run_id] = time.time()

        class FakeStreamResponse:
            def __init__(self, block_first_write=False):
                self.block_first_write = block_first_write
                self.write_started = asyncio.Event()
                self.writes = []

            async def prepare(self, _request):
                return None

            async def write(self, payload):
                if self.block_first_write and payload.startswith(b"data:"):
                    self.write_started.set()
                    await asyncio.Event().wait()
                self.writes.append(payload)

        stale_response = FakeStreamResponse(block_first_write=True)
        replacement_response = FakeStreamResponse()
        stale_request = MagicMock(match_info={"run_id": run_id}, headers={})
        replacement_request = MagicMock(match_info={"run_id": run_id}, headers={})

        with patch(
            "gateway.platforms.api_server.web.StreamResponse",
            side_effect=[stale_response, replacement_response],
        ):
            stale_task = asyncio.create_task(adapter._handle_run_events(stale_request))
            await asyncio.wait_for(stale_response.write_started.wait(), timeout=1.0)
            replacement_task = asyncio.create_task(adapter._handle_run_events(replacement_request))
            await queue.put(None)
            await asyncio.wait_for(replacement_task, timeout=1.0)
            await asyncio.gather(stale_task, return_exceptions=True)

        replacement_body = b"".join(replacement_response.writes)
        assert b'"delta": "preserved"' in replacement_body
        assert b"stream closed" in replacement_body
        assert run_id not in adapter._run_stream_subscribers
        assert run_id not in adapter._run_stream_pending

    @pytest.mark.asyncio

    async def test_overlapping_replacements_serialize_ownership_and_block_ttl_sweep(self, adapter):
        run_id = "run_triple_handoff"
        queue = asyncio.Queue()
        await queue.put({"event": "message.delta", "run_id": run_id, "delta": "initial"})
        adapter._run_streams[run_id] = queue
        adapter._run_streams_created[run_id] = time.time() - adapter._RUN_STREAM_TTL - 1
        release_stale = asyncio.Event()

        class SlowCancelResponse:
            def __init__(self):
                self.write_started = asyncio.Event()

            async def prepare(self, _request):
                return None

            async def write(self, payload):
                if payload.startswith(b"data:"):
                    self.write_started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        await release_stale.wait()
                        raise

        class RecordingResponse:
            def __init__(self):
                self.prepared = asyncio.Event()
                self.writes = []

            async def prepare(self, _request):
                self.prepared.set()

            async def write(self, payload):
                self.writes.append(payload)

        stale_response = SlowCancelResponse()
        middle_response = RecordingResponse()
        final_response = RecordingResponse()
        request = MagicMock(match_info={"run_id": run_id}, headers={})

        with patch(
            "gateway.platforms.api_server.web.StreamResponse",
            side_effect=[stale_response, middle_response, final_response],
        ):
            stale_task = asyncio.create_task(adapter._handle_run_events(request))
            await asyncio.wait_for(stale_response.write_started.wait(), timeout=1.0)
            middle_task = asyncio.create_task(adapter._handle_run_events(request))
            while adapter._run_stream_subscribers.get(run_id) is stale_task:
                await asyncio.sleep(0)
            final_task = asyncio.create_task(adapter._handle_run_events(request))
            while adapter._run_stream_handoffs.get(run_id, 0) < 2:
                await asyncio.sleep(0)

            adapter._sweep_orphaned_runs_once(time.time())
            assert run_id in adapter._run_streams

            release_stale.set()
            await asyncio.wait_for(final_response.prepared.wait(), timeout=1.0)
            await queue.put({"event": "message.delta", "run_id": run_id, "delta": "future"})
            await queue.put(None)
            await asyncio.wait_for(final_task, timeout=1.0)
            await asyncio.gather(stale_task, middle_task, return_exceptions=True)

        assert b'"delta": "future"' in b"".join(final_response.writes)
        assert b'"delta": "future"' not in b"".join(middle_response.writes)
        assert run_id not in adapter._run_stream_subscribers
        assert adapter._run_stream_handoffs.get(run_id, 0) == 0

    @pytest.mark.asyncio

    async def test_subscriber_handoff_restores_terminal_sentinel_interrupted_during_write(self, adapter):
        run_id = "run_sentinel_handoff"
        queue = asyncio.Queue()
        await queue.put(None)
        adapter._run_streams[run_id] = queue
        adapter._run_streams_created[run_id] = time.time()

        class FakeStreamResponse:
            def __init__(self, block_close=False):
                self.block_close = block_close
                self.close_started = asyncio.Event()
                self.writes = []

            async def prepare(self, _request):
                return None

            async def write(self, payload):
                if self.block_close and payload.startswith(b": stream closed"):
                    self.close_started.set()
                    await asyncio.Event().wait()
                self.writes.append(payload)

        stale_response = FakeStreamResponse(block_close=True)
        replacement_response = FakeStreamResponse()
        request = MagicMock(match_info={"run_id": run_id}, headers={})

        with patch(
            "gateway.platforms.api_server.web.StreamResponse",
            side_effect=[stale_response, replacement_response],
        ):
            stale_task = asyncio.create_task(adapter._handle_run_events(request))
            await asyncio.wait_for(stale_response.close_started.wait(), timeout=1.0)
            replacement_task = asyncio.create_task(adapter._handle_run_events(request))
            await asyncio.wait_for(replacement_task, timeout=1.0)
            await asyncio.gather(stale_task, return_exceptions=True)

        assert b"stream closed" in b"".join(replacement_response.writes)
        assert run_id not in adapter._run_stream_pending
        assert run_id not in adapter._run_stream_subscribers

    @pytest.mark.asyncio

    async def test_prepare_failure_releases_subscriber_for_ttl_sweep(self, adapter):
        run_id = "run_prepare_failure"
        adapter._run_streams[run_id] = asyncio.Queue()
        adapter._run_streams_created[run_id] = 0
        request = MagicMock()
        request.match_info = {"run_id": run_id}
        request.headers = {}

        with patch(
            "gateway.platforms.api_server.web.StreamResponse.prepare",
            new=AsyncMock(side_effect=ConnectionResetError("client disconnected")),
        ):
            await adapter._handle_run_events(request)

        assert run_id not in adapter._run_stream_subscribers
        adapter._sweep_orphaned_runs_once(time.time())
        assert run_id not in adapter._run_streams
        assert run_id not in adapter._run_streams_created

    def test_sweep_keeps_transport_with_active_subscriber(self, adapter):
        run_id = "run_subscribed"
        queue = asyncio.Queue()
        adapter._run_streams[run_id] = queue
        adapter._run_streams_created[run_id] = 0
        adapter._run_stream_subscribers[run_id] = MagicMock()

        adapter._sweep_orphaned_runs_once(time.time())

        assert adapter._run_streams[run_id] is queue
        assert run_id in adapter._run_streams_created

    @pytest.mark.asyncio

    async def test_steer_running_agent(self, adapter):
        app = _create_runs_app(adapter)
        agent = MagicMock()
        agent.steer.return_value = True
        queue = asyncio.Queue()
        adapter._active_run_agents["run_123"] = agent
        adapter._run_streams["run_123"] = queue
        adapter._set_run_status("run_123", "running")

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_123/steer", json={"input": "tighten the ending"})
            payload = await resp.json()

        assert resp.status == 200
        assert payload == {
            "object": "hermes.run.steer",
            "run_id": "run_123",
            "accepted": True,
        }
        agent.steer.assert_called_once_with("tighten the ending")
        assert adapter._run_statuses["run_123"]["last_event"] == "run.steered"
        event = queue.get_nowait()
        assert event["event"] == "run.steered"
        assert event["run_id"] == "run_123"
        assert event["accepted"] is True

    @pytest.mark.asyncio
    async def test_steer_nonexistent_run_returns_404(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_missing/steer", json={"input": "hello"})
            payload = await resp.json()

        assert resp.status == 404
        assert payload["error"]["code"] == "run_not_found"

    @pytest.mark.asyncio
    async def test_steer_inactive_run_returns_409(self, adapter):
        app = _create_runs_app(adapter)
        adapter._set_run_status("run_done", "completed")

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_done/steer", json={"input": "hello"})
            payload = await resp.json()

        assert resp.status == 409
        assert payload["error"]["code"] == "run_not_accepting_steer"

    @pytest.mark.asyncio
    async def test_steer_missing_input_returns_400(self, adapter):
        app = _create_runs_app(adapter)
        agent = MagicMock()
        agent.steer.return_value = True
        adapter._active_run_agents["run_123"] = agent
        adapter._set_run_status("run_123", "running")

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_123/steer", json={"input": ""})
            payload = await resp.json()

        assert resp.status == 400
        assert payload["error"]["code"] == "invalid_steer_input"
        agent.steer.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_then_steer_rejects_retained_agent_ref(self, adapter):
        """Steer must reject a stopping run even if the executor thread is still live."""
        app = _create_runs_app(adapter)
        run_can_finish = threading.Event()
        run_started = threading.Event()

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_agent.steer = MagicMock(return_value=True)

                def _interrupt(_message=None):
                    return None

                def _run_conversation(*_args, **_kwargs):
                    run_started.set()
                    run_can_finish.wait(timeout=5)
                    return {"final_response": "late result"}

                mock_agent.interrupt = MagicMock(side_effect=_interrupt)
                mock_agent.run_conversation.side_effect = _run_conversation
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await start_resp.json())["run_id"]
                assert run_started.wait(timeout=3.0)

                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200
                assert run_id in adapter._active_run_agents

                steer_resp = await cli.post(
                    f"/v1/runs/{run_id}/steer",
                    json={"input": "tighten the ending"},
                )
                steer_data = await steer_resp.json()

                assert steer_resp.status == 409
                assert steer_data["error"]["code"] == "run_not_accepting_steer"
                mock_agent.steer.assert_not_called()

                run_can_finish.set()
                for _ in range(40):
                    if run_id not in adapter._active_run_tasks:
                        break
                    await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_pending_steer_preserved_on_run_completed(self, adapter):
        """A steer drained by the turn finalizer (accepted after the final
        response) must surface as pending_steer on the terminal run status
        instead of being silently dropped."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_agent.run_conversation.return_value = {
                    "final_response": "done",
                    "pending_steer": "tighten the ending",
                }
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await start_resp.json())["run_id"]

                for _ in range(40):
                    status = adapter._run_statuses.get(run_id, {})
                    if status.get("status") == "completed":
                        break
                    await asyncio.sleep(0.05)

        assert adapter._run_statuses[run_id]["status"] == "completed"
        assert adapter._run_statuses[run_id]["pending_steer"] == "tighten the ending"

    @pytest.mark.asyncio
    async def test_steer_requires_auth(self, auth_adapter):
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_any/steer", json={"input": "hello"})

        assert resp.status == 401


# ---------------------------------------------------------------------------
# Run lifecycle TTL sweeping
# ---------------------------------------------------------------------------


class TestRunLifecycleSweep:

    @pytest.mark.asyncio
    async def test_expired_live_run_drops_transport_but_keeps_control_state(self, adapter):
        """Stream TTL bounds buffering without detaching a live run."""
        app = _create_runs_app(adapter)
        adapter._max_concurrent_runs = 1

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, _ = _make_slow_agent()
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert start_resp.status == 202
                run_id = (await start_resp.json())["run_id"]
                assert agent_ready.wait(timeout=3.0)

                task = adapter._active_run_tasks[run_id]
                assert isinstance(task, asyncio.Task)
                assert not task.done()

                pending = approval_mod._ApprovalEntry({
                    "command": "bash -c long-running",
                    "description": "approval after stream TTL",
                    "pattern_keys": ["shell-c"],
                })
                with approval_mod._lock:
                    approval_mod._gateway_queues[run_id] = [pending]

                adapter._run_streams_created[run_id] -= adapter._RUN_STREAM_TTL + 1
                # Exercise one real sweeper iteration without waiting 60 seconds.
                with patch(
                    "gateway.platforms.api_server.asyncio.sleep",
                    side_effect=[None, asyncio.CancelledError()],
                ):
                    with pytest.raises(asyncio.CancelledError):
                        await adapter._sweep_orphaned_runs()

                assert adapter._active_run_tasks[run_id] is task
                assert adapter._active_run_agents[run_id] is mock_agent
                assert run_id not in adapter._run_streams
                assert run_id not in adapter._run_streams_created
                assert adapter._run_approval_sessions[run_id] == run_id

                limited = adapter._concurrency_limited_response()
                assert limited is not None
                assert limited.status == 429

                approval_resp = await cli.post(
                    f"/v1/runs/{run_id}/approval",
                    json={"choice": "once"},
                )
                assert approval_resp.status == 200
                assert pending.event.is_set()
                assert pending.result == "once"

                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200
                mock_agent.interrupt.assert_called_once_with("Stop requested via API")


# ---------------------------------------------------------------------------
# POST /v1/runs/{run_id}/stop — interrupt a running agent
# ---------------------------------------------------------------------------


class TestStopRun:

    @pytest.mark.asyncio
    async def test_stop_keeps_uncooperative_executor_tracked_until_exit(self, adapter):
        """Cancelling an asyncio wrapper must not hide its live executor thread."""
        app = _create_runs_app(adapter)
        run_can_finish = threading.Event()
        run_finished = threading.Event()

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                started = threading.Event()

                def _run_conversation(*_args, **_kwargs):
                    started.set()
                    run_can_finish.wait(timeout=5)
                    run_finished.set()
                    return {"final_response": "late result"}

                mock_agent.run_conversation.side_effect = _run_conversation
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await resp.json())["run_id"]
                assert started.wait(timeout=3)

                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200
                await asyncio.sleep(0.1)

                assert not run_finished.is_set()
                assert run_id in adapter._active_run_agents
                assert run_id in adapter._active_run_tasks
                assert adapter._run_statuses[run_id]["status"] == "stopping"

                run_can_finish.set()
                for _ in range(40):
                    if run_id not in adapter._active_run_tasks:
                        break
                    await asyncio.sleep(0.05)

                assert run_id not in adapter._active_run_agents
                assert run_id not in adapter._active_run_tasks
                assert adapter._run_statuses[run_id]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_stop_running_agent(self, adapter):
        """Stop should interrupt the agent and cancel the task."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, _ = _make_slow_agent()
                mock_create.return_value = mock_agent

                # Start run
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                # Wait for agent to start running in the thread
                agent_ready.wait(timeout=3.0)
                await asyncio.sleep(0.1)

                # Verify agent ref is stored
                assert run_id in adapter._active_run_agents

                # Stop the run
                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200
                stop_data = await stop_resp.json()
                assert stop_data["run_id"] == run_id
                assert stop_data["status"] == "stopping"

                # Agent interrupt should have been called
                mock_agent.interrupt.assert_called_once_with("Stop requested via API")

                status_resp = await cli.get(f"/v1/runs/{run_id}")
                assert status_resp.status == 200
                status_data = await status_resp.json()
                assert status_data["status"] in {"stopping", "cancelled"}

                # Refs should be cleaned up
                await asyncio.sleep(0.2)
                assert run_id not in adapter._active_run_agents
                assert run_id not in adapter._active_run_tasks


    @pytest.mark.asyncio
    async def test_stop_sends_sentinel_to_events_stream(self, adapter):
        """After stop, the events stream should close."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, _ = _make_slow_agent()
                mock_create.return_value = mock_agent

                # Start run
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                agent_ready.wait(timeout=3.0)
                await asyncio.sleep(0.1)

                # Subscribe to events in background
                events_task = asyncio.ensure_future(
                    cli.get(f"/v1/runs/{run_id}/events")
                )

                await asyncio.sleep(0.1)

                # Stop the run
                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200

                # Events stream should close
                events_resp = await asyncio.wait_for(events_task, timeout=5.0)
                assert events_resp.status == 200
                body = await events_resp.text()
                # Stream should have received run.failed and closed
                assert "run.failed" in body or "stream closed" in body


class TestRunsProviderAuthFailure:
    @pytest.mark.asyncio
    async def test_status_reports_provider_auth_failure_distinctly(self, adapter):
        """/v1/runs builds its own agent via _create_agent() and does not
        route through _run_agent(), so the controlled "Provider
        authentication failed" message added there does not cover this
        endpoint. _handle_runs()'s own _ProviderAuthResolutionError branch
        must give the same distinguished message instead of the generic
        except-Exception "run failed" text."""
        from gateway.platforms.api_server import _ProviderAuthResolutionError

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_create.side_effect = _ProviderAuthResolutionError(
                    "No credentials found for provider 'nous'"
                )

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                for _ in range(40):
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    status = await status_resp.json()
                    if status["status"] == "failed":
                        break
                    await asyncio.sleep(0.05)

                assert status["status"] == "failed"
                assert status["error"] == "⚠️ Provider authentication failed: No credentials found for provider 'nous'"
                assert status["last_event"] == "run.failed"
