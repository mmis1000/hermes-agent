"""Provider-safe, child-only transcript hydration for resumed delegations."""

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _session(db, sid, *, parent=None, source="subagent", owner="parent"):
    config = {"_delegate_from": owner} if source == "subagent" else None
    db.create_session(
        sid,
        source=source,
        parent_session_id=parent,
        model_config=config,
    )


def _metadata(child="child-1", parent="parent"):
    return {
        "child_session_id": child,
        "parent_session_id": parent,
        "model": "provider/model",
        "provider": "provider",
        "role": "leaf",
        "depth": 1,
    }


def test_hydrates_child_only_resume_chain_and_preserves_provider_fields(db):
    _session(db, "parent", source="discord", owner=None)
    db.append_message("parent", "user", "PARENT PRIVATE TRANSCRIPT")
    _session(db, "child-1", parent="parent")
    db.append_message("child-1", "user", "initial child goal")
    tool_calls = [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"x"}'},
        }
    ]
    db.append_message(
        "child-1",
        "assistant",
        "working",
        tool_calls=tool_calls,
        reasoning="private reasoning",
        reasoning_content="provider reasoning",
        reasoning_details=[{"type": "reasoning.text", "text": "detail"}],
        codex_reasoning_items=[{"type": "reasoning", "id": "r1"}],
        codex_message_items=[{"type": "message", "id": "m1"}],
        api_content="exact provider payload ",
    )
    db.append_message(
        "child-1", "tool", "file data", tool_call_id="call-1", tool_name="read_file"
    )
    db.append_message("child-1", "assistant", "attempt one done")

    # A resumed attempt is a new child segment linked to the prior segment.
    _session(db, "child-2", parent="child-1")
    db.append_message("child-2", "user", "continue with the result")
    db.append_message("child-2", "assistant", "attempt two done")

    bundle = db.get_subagent_resume_bundle("child-2", _metadata(child="child-2"))

    assert bundle["child_lineage"] == ["child-1", "child-2"]
    assert bundle["prior_child_session_id"] == "child-2"
    assert "PARENT PRIVATE TRANSCRIPT" not in repr(bundle["history"])
    assert [m["content"] for m in bundle["history"] if m["role"] == "user"] == [
        "initial child goal",
        "continue with the result",
    ]
    replayed = next(m for m in bundle["history"] if m.get("content") == "working")
    assert replayed["tool_calls"] == tool_calls
    assert replayed["reasoning"] == "private reasoning"
    assert replayed["reasoning_content"] == "provider reasoning"
    assert replayed["reasoning_details"] == [
        {"type": "reasoning.text", "text": "detail"}
    ]
    assert replayed["codex_reasoning_items"] == [{"type": "reasoning", "id": "r1"}]
    assert replayed["codex_message_items"] == [{"type": "message", "id": "m1"}]
    assert replayed["api_content"] == "exact provider payload "

    from tools.delegate_tool import prepare_resumed_child_session

    continuation_a = prepare_resumed_child_session(bundle)
    continuation_b = prepare_resumed_child_session(bundle)
    assert continuation_a["session_id"] != continuation_b["session_id"]
    assert continuation_a["parent_session_id"] == "child-2"
    assert continuation_a["delegate_from"] == "parent"


def test_follows_marked_child_compression_and_sanitizes_dangling_tool_tail(db):
    _session(db, "parent", source="discord", owner=None)
    _session(db, "child-1", parent="parent")
    db.append_message("child-1", "user", "goal")
    db.append_message("child-1", "assistant", "pre-compression")
    db.end_session("child-1", "compression")
    _session(db, "child-compressed", parent="child-1")
    db.append_message("child-compressed", "user", "after compression")
    db.append_message(
        "child-compressed",
        "assistant",
        "",
        tool_calls=[
            {
                "id": "dangling",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    )

    bundle = db.get_subagent_resume_bundle("child-1", _metadata())

    assert bundle["prior_child_session_id"] == "child-compressed"
    assert bundle["child_lineage"] == ["child-1", "child-compressed"]
    assert bundle["history"][-1]["role"] == "user"
    assert bundle["history"][-1]["content"] == "after compression"


def test_reconstructed_child_uses_saved_policy_but_live_credentials(monkeypatch):
    from types import SimpleNamespace
    from tools import delegate_tool

    captured = {}

    def fake_resolve(config, parent_agent):
        captured["credential_config"] = config
        captured["credential_parent"] = parent_agent
        return {
            "provider": "test-provider",
            "api_key": "live-key-never-persisted",
            "base_url": "https://current.example",
            "api_mode": "chat_completions",
            "request_overrides": {"safe": True},
            "max_output_tokens": 999,
            "command": None,
            "args": None,
        }

    def fake_build(**kwargs):
        captured["build"] = kwargs
        return SimpleNamespace(
            session_id="throwaway",
            _parent_session_id="throwaway-parent",
            _subagent_id="throwaway-id",
            _parent_subagent_id=None,
            _delegate_depth=1,
            _delegate_role="leaf",
            _subagent_goal="",
            _delegation_session_ref={},
            _delegation_runtime_metadata={},
            _session_init_model_config={},
            enabled_toolsets=["file"],
            tool_progress_callback=None,
            thinking_callback=None,
        )

    callback = lambda *_args, **_kwargs: None
    monkeypatch.setattr(delegate_tool, "_resolve_delegation_credentials", fake_resolve)
    monkeypatch.setattr(delegate_tool, "_build_child_agent", fake_build)
    monkeypatch.setattr(
        delegate_tool, "_build_child_progress_callback", lambda *a, **kw: callback
    )
    parent = object()
    bundle = {
        "prior_child_session_id": "child-old",
        "owner_parent_session_id": "parent-owner",
        "reconstruction_metadata": {
            "child_session_id": "child-old",
            "parent_session_id": "parent-owner",
            "parent_logical_id": "sa-parent",
            "provider": "test-provider",
            "model": "test-model",
            "role": "leaf",
            "depth": 2,
            "enabled_toolsets": ["file"],
            "disabled_toolsets": ["delegation", "terminal"],
            "workdir": "/safe/workdir",
            "max_iterations": 17,
            "max_tokens": 321,
            "reasoning_config": {"effort": "low"},
            "fallback_routes": [{"provider": "fallback", "model": "fallback-model"}],
            "provider_preferences": {"allowed": ["one"], "sort": "price"},
        },
    }
    continuation = {
        "session_id": "child-new",
        "parent_session_id": "child-old",
        "delegate_from": "parent-owner",
    }

    child = delegate_tool.build_resumed_child_agent(
        bundle=bundle,
        logical_id="sa-stable",
        goal="finish it",
        parent_agent=parent,
        continuation=continuation,
    )

    assert captured["credential_config"] == {
        "provider": "test-provider",
        "model": "test-model",
    }
    assert captured["credential_parent"] is parent
    build = captured["build"]
    assert build["override_api_key"] == "live-key-never-persisted"
    assert build["required_disabled_toolsets"] == ["delegation", "terminal"]
    assert build["workspace_override"] == "/safe/workdir"
    assert build["reasoning_config_override"] == {"effort": "low"}
    assert build["fallback_model_override"] == [
        {"provider": "fallback", "model": "fallback-model"}
    ]
    assert build["provider_preferences_override"] == {
        "allowed": ["one"],
        "sort": "price",
    }
    assert build["override_max_tokens"] == 321
    assert child.session_id == "child-new"
    assert child._parent_session_id == "child-old"
    assert child._subagent_id == "sa-stable"
    assert child._parent_subagent_id == "sa-parent"
    assert child._delegate_depth == 2
    assert child._session_init_model_config["_delegate_from"] == "parent-owner"
    assert child._delegation_runtime_metadata["child_session_id"] == "child-new"
    assert "live-key-never-persisted" not in repr(child._delegation_runtime_metadata)
    assert child.tool_progress_callback is callback


def test_async_loader_is_authorized_and_keeps_provider_history_internal(
    tmp_path, monkeypatch
):
    from tools import async_delegation as ad

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    db = SessionDB()
    _session(db, "parent", source="discord", owner=None)
    _session(db, "child-1", parent="parent")
    db.append_message("child-1", "user", "goal")
    db.append_message(
        "child-1", "assistant", "answer", reasoning="private chain"
    )
    repository = ad._repository()
    initial = repository.register_initial_dispatch(
        {
            "delegation_id": "deleg-hydrate",
            "session_key": "parent",
            "dispatched_at": 1.0,
            "root_subagent_ids": ["logical-child"],
        }
    )
    attempt = initial["attempts"][0]
    assert repository.transition_attempt(
        attempt["attempt_id"],
        {"starting"},
        "completed",
        metadata=_metadata(),
        completed_at=2.0,
    )["status"] == "updated"

    loaded = ad.load_subagent_resume_bundle(
        "deleg-hydrate", "logical-child", session_key="parent"
    )
    foreign = ad.load_subagent_resume_bundle(
        "deleg-hydrate", "logical-child", session_key="foreign"
    )
    projected = ad.get_async_delegation("deleg-hydrate", session_key="parent")

    assert loaded["status"] == "ready"
    assert loaded["bundle"]["history"][-1]["reasoning"] == "private chain"
    assert foreign == {"status": "not_found"}
    assert "history" not in repr(projected)
    assert "private chain" not in repr(projected)
    ad._reset_for_tests()


@pytest.mark.parametrize(
    "mutation, error",
    [
        ("missing", "missing subagent transcript"),
        ("wrong-source", "foreign subagent lineage"),
        ("wrong-marker", "foreign subagent lineage"),
        ("foreign-parent", "foreign subagent lineage"),
        ("missing-metadata", "incomplete reconstruction metadata"),
    ],
)
def test_resume_bundle_fails_closed(db, mutation, error):
    _session(db, "parent", source="discord", owner=None)
    child = "missing" if mutation == "missing" else "child-1"
    if mutation != "missing":
        source = "cli" if mutation == "wrong-source" else "subagent"
        owner = "other-parent" if mutation in {"wrong-marker", "foreign-parent"} else "parent"
        parent = "other-parent" if mutation == "foreign-parent" else "parent"
        if parent == "other-parent":
            _session(db, "other-parent", source="discord", owner=None)
        _session(db, child, parent=parent, source=source, owner=owner)
        db.append_message(child, "user", "goal")
    metadata = _metadata(child=child)
    if mutation == "missing-metadata":
        metadata.pop("parent_session_id")

    with pytest.raises(ValueError, match=error):
        db.get_subagent_resume_bundle(child, metadata)
