from __future__ import annotations

from pathlib import Path

import pytest


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


def test_direct_message_task_preserves_raw_plural_text(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    mgr = TaskIntentManager("sid-direct-raw")
    state = mgr.record_direct_message("add more styles")

    assert state.kind == "direct_user_message"
    assert state.raw_text == "add more styles"
    assert state.task_contract.raw_primary_text == "add more styles"
    assert state.task_contract.derived_metadata["multiplicity_required"] is True
    assert "more" in state.task_contract.derived_metadata["multiplicity_terms"]
    assert "styles" in state.task_contract.derived_metadata["multiplicity_terms"]

    reloaded = TaskIntentManager("sid-direct-raw").state
    assert reloaded is not None
    assert reloaded.raw_text == "add more styles"


def test_direct_message_completion_vetoes_one_slice_done(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    mgr = TaskIntentManager("sid-direct-veto")
    mgr.record_direct_message("add more styles")

    decision = mgr.evaluate_after_response("I added one style. Done.")

    assert decision["verdict"] == "continue"
    assert decision["guard_vetoed_done"] is True
    assert mgr.state is not None
    assert mgr.state.status == "active"
    assert "one slice" in decision["reason"] or "multiple" in decision["reason"]


def test_direct_message_explicit_scope_reduction_allows_one_slice_done(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    mgr = TaskIntentManager("sid-direct-scope")
    mgr.record_direct_message("add more styles")
    mgr.record_direct_message("actually just add one style")

    decision = mgr.evaluate_after_response("I added one style. Done.")

    assert decision["verdict"] == "done"
    assert decision["guard_vetoed_done"] is False
    assert mgr.state is not None
    assert mgr.state.status == "completed"


def test_supplement_preserves_original_and_replacement_warns_with_raw_text(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    mgr = TaskIntentManager("sid-direct-rel")
    mgr.record_direct_message("add more styles")
    supplement = mgr.record_direct_message("also make them pastel")

    assert supplement.relationship_to_active_task == "supplement"
    assert supplement.task_contract.raw_primary_text == "add more styles"
    assert "also make them pastel" in supplement.task_contract.raw_supplements

    replacement = mgr.record_direct_message("switch to debugging install instead")

    assert replacement.relationship_to_active_task == "replacement"
    assert replacement.status == "active"
    assert replacement.last_relevance_check is not None
    assert replacement.last_relevance_check["verdict"] == "replacement"
    assert "add more styles" in replacement.last_relevance_check["previous_raw_text"]


def test_unrelated_new_direct_task_warns_with_previous_raw_text(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    mgr = TaskIntentManager("sid-direct-new-task")
    mgr.record_direct_message("add more styles")
    new_task = mgr.record_direct_message("check whether docker is running")

    assert new_task.relationship_to_active_task == "new_task"
    assert new_task.last_relevance_check is not None
    assert new_task.last_relevance_check["verdict"] == "new_task"
    notice = mgr.format_preserved_state_notice()
    assert "add more styles" in notice
    assert "looks like a new task" in notice


def test_machine_preserved_message_raw_text_is_canonical_and_displayed(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    raw_machine = "[Continuing previous direct-message task]\nContinue adding more styles."
    mgr = TaskIntentManager("sid-direct-machine")
    mgr.record_direct_message("add more styles")
    item = mgr.add_machine_preserved_message(raw_machine, origin="direct_message_continuation")

    assert item.raw_text == raw_machine
    display = mgr.format_preserved_state_notice()
    assert raw_machine in display
    assert "Machine-preserved ongoing message" in display
