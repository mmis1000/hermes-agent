"""R1: real TUI branch copy, identity-bounded canonical reconstruction."""
import json

import pytest

from conftest import record, decision
from task_context.migration import ensure_import
from task_context.store import Store
from task_context.policy import TaskIntentState, TaskContract


def accept(db, store, text, occurrence, *, relation='supplement', effect='append_contract', link=True, session='parent'):
    item = record(text, occurrence)
    store.admit(session, [item])
    store.drain(session, lambda s, r: decision(relation, effect, r['text']))
    db.append_message(session, role='user', content=text,
                      display_metadata={'input_provenance': [item]})
    row = db.get_messages_as_conversation(session)[-1]
    if link:
        store.link(session, [item], row['message_uid'])
    return row


def fork(db, tmp_path, history, *, copy_metadata=False, new='branch', parent='parent'):
    from tui_gateway.methods_session import _persist_branch
    if not copy_metadata:
        # Modern branch writers always retain constituent metadata. Strip it
        # from fixture copies to exercise historical UID-only compatibility.
        history = [{key: value for key, value in row.items() if key != 'display_metadata'} for row in history]
    _persist_branch(db, new, parent, 'branch test', history, source='cli', cwd=str(tmp_path),
                    profile_name='default', model='test',
                    copy_fields=('display_metadata',) if copy_metadata else ())
    copied = db.get_messages_as_conversation(new)
    assert [r['message_uid'] for r in copied] == [r['message_uid'] for r in history]
    assert db.get_session(parent)['ended_at'] is None
    return copied


@pytest.mark.parametrize('copy_metadata', [False, True])
def test_tui_late_first_import_does_not_copy_later_parent_input(tmp_path, copy_metadata):
    from hermes_state import SessionDB
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('parent', source='cli')
        store = Store(tmp_path)
        ensure_import(store, 'parent', db)
        original = accept(db, store, 'Primary A and B', 'primary', link=not copy_metadata)
        expected_id = store.contract('parent')['id']
        copied = fork(db, tmp_path, [original], copy_metadata=copy_metadata)
        if not copy_metadata:
            assert not copied[0].get('display_metadata', {}).get('input_provenance')
        accept(db, store, 'Late parent-only supplement', 'late')
        ensure_import(store, 'branch', db)
        assert store.contract('branch')['raw_supplements'] == []
        assert store.contract('branch')['id'] == expected_id
        assert store.contract('parent')['raw_supplements'] == ['Late parent-only supplement']
        store.admit('branch', [record('Primary A and B', 'primary')])
        store.drain('branch', lambda *_: pytest.fail('copied receipt reclassified'))
        accept(db, store, 'Branch-only supplement', 'branch-new', session='branch')
        assert store.contract('branch')['raw_supplements'] == ['Branch-only supplement']
        assert store.contract('parent')['raw_supplements'] == ['Late parent-only supplement']
        snapshot = store.contract('branch')
        ensure_import(store, 'branch', db)
        assert store.contract('branch') == snapshot
        with store.connection() as conn:
            receipt = dict(conn.execute('SELECT * FROM branch_boundaries WHERE session_id=?', ('branch',)).fetchone())
            occurrences = [r[0] for r in conn.execute('SELECT occurrence_id FROM inputs WHERE session_id=? ORDER BY seq', ('branch',))]
        assert receipt['through_occurrence_id'] == 'primary'
        assert receipt['source'] == ('copied_occurrence_ids' if copy_metadata else 'copied_message_uids')
        assert receipt['parent_owner'] == 'parent'
        assert occurrences == ['primary', 'branch-new']


def test_prefix_keeps_compacted_away_older_obligations(tmp_path):
    from hermes_state import SessionDB
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('parent', source='cli')
        store = Store(tmp_path)
        ensure_import(store, 'parent', db)
        accept(db, store, 'Old primary A and B', 'primary')
        original_id = store.contract('parent')['id']
        accept(db, store, 'Older unresolved detail', 'old-detail')
        recent = accept(db, store, 'Related question at branch boundary', 'recent',
                        relation='related_question', effect='no_change')
        # Real durable compacted-history shape: older input rows are archived,
        # absent from the branch copy; only the later identity establishes cutoff.
        db.replace_messages('parent', [
            {'role': 'assistant', 'content': '[CONTEXT SUMMARY]: compacted history', '_compressed_summary': True},
            recent], archive_dropped=True)
        history = db.get_messages_as_conversation('parent')
        assert not any(r.get('content') == 'Old primary A and B' for r in history)
        fork(db, tmp_path, history)
        accept(db, store, 'Too late for this branch', 'late')
        ensure_import(store, 'branch', db)
        contract = store.contract('branch')
        assert contract['id'] == original_id
        assert contract['raw_primary_text'] == 'Old primary A and B'
        assert contract['raw_supplements'] == ['Older unresolved detail']
        with store.connection() as conn:
            rows = conn.execute('SELECT occurrence_id,processed FROM inputs WHERE session_id=? ORDER BY seq', ('branch',)).fetchall()
            boundary = conn.execute('SELECT through_occurrence_id FROM branch_boundaries WHERE session_id=?', ('branch',)).fetchone()[0]
        assert [(r[0], r[1]) for r in rows] == [('primary', 1), ('old-detail', 1), ('recent', 1)]
        assert boundary == 'recent'
        store.drain('branch', lambda *_: pytest.fail('prefix replay invoked classifier'))


@pytest.mark.parametrize('late_relation,late_effect', [('replacement', 'supersede'), ('cancellation', 'cancel')])
def test_legacy_anchor_and_existing_task_ids_survive_bounded_replay(tmp_path, late_relation, late_effect):
    from hermes_state import SessionDB
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('parent', source='cli')
        legacy = TaskIntentState(id='legacy-original', created_at=1, updated_at=1, status='active',
                                 task_contract=TaskContract('Legacy primary', ['Legacy old supplement']))
        db.set_meta('task_intent:parent', legacy.to_json())
        store = Store(tmp_path)
        ensure_import(store, 'parent', db)
        accept(db, store, 'Additional independent task', 'second-task', relation='new_task', effect='start_new')
        recent = accept(db, store, 'new task supplement', 'recent')
        expected = store.contract('parent')
        fork(db, tmp_path, [recent])
        accept(db, store, 'Late control', 'late', relation=late_relation, effect=late_effect)
        ensure_import(store, 'branch', db)
        actual = store.contract('branch')
        for key in ('id', 'status', 'raw_primary_text', 'raw_supplements', 'additional_active_task_ids'):
            assert actual[key] == expected[key]
        assert actual['additional_active_task_ids'] == ['legacy-original']
        assert {s.id for s in store.snapshot('branch')[1]} == {'legacy-original', expected['id']}
        store.drain('branch', lambda *_: pytest.fail('imported prefix reclassified'))
