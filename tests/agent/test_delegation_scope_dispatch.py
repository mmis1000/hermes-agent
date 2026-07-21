from unittest.mock import patch

import pytest

from run_agent import AIAgent
from tools.registry import registry


@pytest.mark.parametrize(
    "payload",
    [
        {"goal": "single", "context": "plain prose"},
        {"tasks": [{"goal": "one"}, {"goal": "two", "context": "notes"}]},
    ],
)
def test_aiagent_dispatch_forwards_top_level_scope_for_single_and_batch(payload):
    agent = AIAgent.__new__(AIAgent)
    agent._delegate_depth = 0
    args = {
        **payload,
        "profile": "isolated",
        "workdir": "/workspace/job",
        "reveal": [{"path": "/work/input", "mode": "ro"}],
    }

    with patch("tools.delegate_tool.delegate_task", return_value="ok") as delegated:
        assert agent._dispatch_delegate_task(args) == "ok"

    kwargs = delegated.call_args.kwargs
    assert kwargs["profile"] == "isolated"
    assert kwargs["workdir"] == "/workspace/job"
    assert kwargs["reveal"] == [{"path": "/work/input", "mode": "ro"}]
    if "goal" in payload:
        assert kwargs["goal"] == "single"
        assert kwargs["context"] == "plain prose"
    else:
        assert [task["goal"] for task in kwargs["tasks"]] == ["one", "two"]


def test_registry_fallback_forwards_top_level_scope():
    entry = registry.get_entry("delegate_task")
    assert entry is not None
    parent = object()
    args = {
        "goal": "plain goal",
        "context": "plain context",
        "profile": "isolated",
        "workdir": "/workspace/job",
        "reveal": [{"path": "/work/input", "mode": "rw"}],
    }
    with patch("tools.delegate_tool.delegate_task", return_value="ok") as delegated:
        assert entry.handler(args, parent_agent=parent) == "ok"
    kwargs = delegated.call_args.kwargs
    assert (kwargs["profile"], kwargs["workdir"], kwargs["reveal"]) == (
        "isolated",
        "/workspace/job",
        [{"path": "/work/input", "mode": "rw"}],
    )
