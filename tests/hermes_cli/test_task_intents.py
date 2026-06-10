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
    assert [m.raw_text for m in state.raw_messages] == ["add more styles"]
    assert state.raw_messages[0].relationship_to_active_task == "new_task"

    reloaded = TaskIntentManager("sid-direct-raw").state
    assert reloaded is not None
    assert reloaded.raw_text == "add more styles"
    assert reloaded.task_contract.raw_primary_text == "add more styles"
    assert [m.raw_text for m in reloaded.raw_messages] == ["add more styles"]


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
    state = mgr.record_direct_message("actually just add one style")

    assert state.relationship_to_active_task == "replacement"
    assert state.task_contract.raw_primary_text == "actually just add one style"

    decision = mgr.evaluate_after_response("I added one style. Done.")

    assert decision["verdict"] == "done"
    assert decision["guard_vetoed_done"] is False
    assert mgr.state is not None
    assert mgr.state.status == "completed"


def test_supplement_preserves_original_and_appends_raw_without_rewrite(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    mgr = TaskIntentManager("sid-direct-rel")
    mgr.record_direct_message("add more styles")
    supplement = mgr.record_direct_message("also make them pastel")

    assert supplement.relationship_to_active_task == "supplement"
    assert supplement.task_contract.raw_primary_text == "add more styles"
    assert "also make them pastel" in supplement.task_contract.raw_supplements
    assert [m.raw_text for m in supplement.raw_messages] == [
        "add more styles",
        "also make them pastel",
    ]


def test_conservative_followups_do_not_become_new_tasks_without_explicit_replacement(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    mgr = TaskIntentManager("sid-direct-conservative")
    mgr.record_direct_message("add more styles")
    first = mgr.record_direct_message("what flag did you use?")
    second = mgr.record_direct_message("then materialize the full plan")

    assert first.relationship_to_active_task == "supplement"
    assert second.relationship_to_active_task == "supplement"
    assert second.task_contract.raw_primary_text == "add more styles"
    assert "what flag did you use?" in second.task_contract.raw_supplements
    assert "then materialize the full plan" in second.task_contract.raw_supplements
    assert [m.raw_text for m in second.raw_messages] == [
        "add more styles",
        "what flag did you use?",
        "then materialize the full plan",
    ]
    assert second.last_relevance_check is None


def test_explicit_replacement_warns_with_previous_raw_text(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    mgr = TaskIntentManager("sid-direct-replacement")
    mgr.record_direct_message("add more styles")
    replacement = mgr.record_direct_message("stop that and switch to debugging install instead")

    assert replacement.relationship_to_active_task == "replacement"
    assert replacement.status == "active"
    assert replacement.task_contract.raw_primary_text == "stop that and switch to debugging install instead"
    assert replacement.last_relevance_check is not None
    assert replacement.last_relevance_check["verdict"] == "replacement"
    assert replacement.last_relevance_check["previous_raw_text"] == "add more styles"
    notice = mgr.format_preserved_state_notice()
    assert "add more styles" in notice
    assert "stop that" not in replacement.last_relevance_check["previous_raw_text"]


def test_clamp_raw_text_uses_exact_prefix_ellipsis_suffix_without_normalizing():
    from hermes_cli.task_intents import clamp_raw_text

    raw = "PREFIX  Keep CASE and punctuation!!! middle text that should disappear  suffix?"
    clamped = clamp_raw_text(raw, 25)

    assert clamped == raw[:12] + "…" + raw[-12:]
    assert "…" in clamped
    assert clamped.startswith("PREFIX  Keep")
    assert clamped.endswith("ear  suffix?")


def test_judge_payload_rewrite_fields_are_rejected_and_quotes_must_be_exact():
    from hermes_cli.task_intents import validate_judge_payload_no_rewrite

    raw = "add more styles"
    cleaned = validate_judge_payload_no_rewrite(
        {
            "relationship": "supplement",
            "summary": "style expansion",
            "rewritten_task": "create additional visual styles",
            "evidence_quotes": ["more styles", "additional styles"],
        },
        raw_texts=[raw],
    )

    assert cleaned["relationship"] == "supplement"
    assert "summary" not in cleaned
    assert "rewritten_task" not in cleaned
    assert cleaned["evidence_quotes"] == ["more styles"]
    assert set(cleaned["_rejected_rewrite_keys"]) == {"summary", "rewritten_task"}


def test_machine_preserved_message_raw_text_is_canonical_and_displayed(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    raw_machine = "[Continuing previous direct-message task]\nContinue adding more styles."
    mgr = TaskIntentManager("sid-direct-machine")
    mgr.record_direct_message("add more styles")
    item = mgr.add_machine_preserved_message(raw_machine, origin="direct_message_continuation")

    assert item.raw_text == raw_machine
    assert mgr.state is not None
    assert mgr.state.raw_messages[-1].source == "machine"
    assert mgr.state.raw_messages[-1].raw_text == raw_machine
    display = mgr.format_preserved_state_notice()
    assert raw_machine in display
    assert "Machine-preserved ongoing message" in display


def test_machine_preserved_plural_contract_can_veto_one_slice_done(hermes_home):
    from hermes_cli.task_intents import TaskIntentManager

    mgr = TaskIntentManager("sid-direct-machine-veto")
    mgr.record_direct_message("continue")
    mgr.add_machine_preserved_message(
        "[Continuing previous direct-message task]\nContinue adding more styles.",
        origin="direct_message_continuation",
    )

    decision = mgr.evaluate_after_response("I added one style. Done.")

    assert decision["verdict"] == "continue"
    assert decision["guard_vetoed_done"] is True
