"""L1: UID-only pre-journal branches may inherit a proven legacy anchor."""
import json

import pytest

from conftest import decision, record
from task_context.migration import ensure_import, read_task_contract
from task_context.policy import TaskContract, TaskIntentState
from task_context.store import Store
from test_branch_boundaries import accept, fork


def legacy_branch(db, home, *, copied_occurrence=False, legacy=True):
    db.create_session('parent', source='cli')
    db.append_message('parent', role='user', content='Legacy primary')
    row = db.get_messages_as_conversation('parent')[-1]
    assert row['message_uid']
    if copied_occurrence:
        row['display_metadata'] = {'input_provenance': [record('Legacy primary', 'unknown')]}
    fork(db, home, [row], copy_metadata=copied_occurrence)
    assert db.get_meta('task_intent:branch') is None
    if legacy:
        state = TaskIntentState(id='legacy-original', created_at=1, updated_at=1, status='active',
                                task_contract=TaskContract('Legacy primary', ['Legacy supplement']))
        db.set_meta('task_intent:parent', state.to_json())
    store = Store(home)
    ensure_import(store, 'parent', db)
    return store, row


@pytest.mark.parametrize('effect,relation', [('append_contract', 'supplement'), ('supersede', 'replacement')])
def test_linked_human_journal_excluded_from_legacy_anchor(tmp_path, effect, relation):
    from hermes_state import SessionDB
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        store, copied = legacy_branch(db, tmp_path)
        accept(db, store, 'Post-cutover input', 'late', effect=effect, relation=relation)
        parent_export = json.loads(store.export_legacy('parent'))['task_context_export']
        result = read_task_contract(session_id='branch', session_db=db, hermes_home=tmp_path)
        assert result['status'] == 'ok'
        assert result['contract']['id'] == 'legacy-original'
        assert result['contract']['status'] == 'active'
        assert result['contract']['raw_primary_text'] == 'Legacy primary'
        assert result['contract']['raw_supplements'] == ['Legacy supplement']
        assert store.imported('branch')
        exported = store.export_legacy('branch')
        extension = json.loads(exported)['task_context_export']
        assert extension['inputs'] == extension['links'] == []
        assert extension['anchor'] == parent_export['anchor']
        receipt = extension['branch_boundary']
        assert receipt['source'] == 'pre_journal_anchor'
        assert receipt['through_occurrence_id'] is None and receipt['through_seq'] is None
        assert json.loads(receipt['witnesses'])['message_uids'] == [copied['message_uid']]
        # Reopening does not follow subsequent parent mutations or invoke judges.
        accept(db, store, 'Even later', 'later')
        ensure_import(store, 'branch', db)
        store.drain('branch', lambda *_: pytest.fail('legacy import invoked classifier'))
        assert store.export_legacy('branch') == exported
        restored = Store(tmp_path / 'restored')
        assert restored.import_legacy('branch', exported)
        assert not restored.import_legacy('branch', exported)
        restored_extension = json.loads(restored.export_legacy('branch'))['task_context_export']
        for key in ('tasks', 'anchor', 'branch_boundary', 'inputs', 'links'):
            assert restored_extension[key] == extension[key]
        assert restored.contract('branch')['id'] == 'legacy-original'
        assert json.loads(store.export_legacy('parent'))['task_context_export']['inputs'][0] == parent_export['inputs'][0]


@pytest.mark.parametrize('missing', ['unlinked', 'unlinked_no_change', 'pending', 'occurrence', 'empty_anchor', 'missing_anchor', 'later_anchor'])
def test_unproved_pre_journal_boundary_stays_degraded(tmp_path, missing):
    from hermes_state import SessionDB
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        store, _ = legacy_branch(db, tmp_path, copied_occurrence=missing == 'occurrence',
                                 legacy=missing != 'empty_anchor')
        if missing in {'unlinked', 'unlinked_no_change', 'pending'}:
            accept(db, store, 'Other fully linked journal input', 'linked')
        if missing == 'pending':
            item = record('Unpublished human input', 'late')
            store.admit('parent', [item])
            db.append_message('parent', role='user', content=item['text'])
            store.link('parent', [item], db.get_messages_as_conversation('parent')[-1]['message_uid'])
        else:
            accept(db, store, 'Post-cutover input', 'late', link=not missing.startswith('unlinked'),
                   effect='no_change' if missing == 'unlinked_no_change' else 'append_contract',
                   relation='related_question' if missing == 'unlinked_no_change' else 'supplement')
        if missing in {'missing_anchor', 'later_anchor'}:
            with store.connection(write=True) as conn:
                if missing == 'missing_anchor':
                    conn.execute('DELETE FROM anchors WHERE session_id=?', ('parent',))
                else:
                    conn.execute('UPDATE anchors SET through_occurrence_id=? WHERE session_id=?', ('late', 'parent'))
        before = store.export_legacy('parent')
        result = read_task_contract(session_id='branch', session_db=db, hermes_home=tmp_path)
        assert result['status'] == 'error'
        assert not store.imported('branch')
        assert store.contract('branch') is None
        assert store.export_legacy('parent') == before
        with store.connection() as conn:
            assert conn.execute('SELECT 1 FROM branch_boundaries WHERE session_id=?', ('branch',)).fetchone() is None
        # Missing publication/link evidence remains retryable, not imported empty.
        if missing == 'pending':
            store.drain('parent', lambda s, r: decision('supplement', 'append_contract', r['text']))
        elif missing.startswith('unlinked'):
            store.link('parent', [record('Post-cutover input', 'late')], db.get_messages_as_conversation('parent')[-1]['message_uid'])
        else:
            return
        retried = read_task_contract(session_id='branch', session_db=db, hermes_home=tmp_path)
        assert retried['status'] == 'ok'
        assert retried['contract']['id'] == 'legacy-original'
        assert retried['contract']['raw_supplements'] == ['Legacy supplement']
