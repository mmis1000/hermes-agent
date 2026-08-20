"""Regression coverage for public helpers lost during branch rebases."""


def test_non_test_callers_can_import_their_runtime_helpers():
    from tools.async_delegation import (
        _current_origin_session_id,
        active_task_count,
        drop_completion_delivery,
        has_live_for_session,
    )
    from tools.delegate_tool import (
        _build_child_preserving_parent_tools,
        _run_child_lifecycle,
        steer_subagent,
    )

    assert all(
        callable(item)
        for item in (
            _current_origin_session_id,
            active_task_count,
            drop_completion_delivery,
            has_live_for_session,
            _build_child_preserving_parent_tools,
            _run_child_lifecycle,
            steer_subagent,
        )
    )