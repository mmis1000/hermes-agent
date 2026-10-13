"""Branch boundary replay durability and conservative failure modes."""
import json

import pytest

from conftest import record, decision
from task_context.migration import ensure_import, read_task_contract
from task_context.store import Store
from test_branch_boundaries import accept, fork


def test_replays_prior_decision_format_without_minting_task_ids(tmp_path):
    from hermes_state import SessionDB
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('parent', source='cli')
        store = Store(tmp_path)
        ensure_import(store, 'parent', db)
        row = accept(db, store, 'Primary', 'first')
        task_id = store.contract('parent')['id']
        fork(db, tmp_path, [row])
        accept(db, store, 'New parent replacement', 'late', relation='replacement', effect='supersede')
        with store.connection(write=True) as conn:
            for item in conn.execute('SELECT seq,decision FROM inputs').fetchall():
                annotation = json.loads(item['decision'])
                annotation.pop('applied_at', None)
                annotation.pop('created_task_id', None)
                conn.execute('UPDATE inputs SET decision=? WHERE seq=?', (json.dumps(annotation), item['seq']))
        ensure_import(store, 'branch', db)
        assert store.contract('branch')['id'] == task_id
        assert store.contract('branch')['raw_primary_text'] == 'Primary'
        assert store.contract('branch')['status'] == 'active'


def test_export_roundtrip_retains_anchor_for_earlier_branch_boundary(tmp_path):
    from hermes_state import SessionDB
    source, target = tmp_path / 'source', tmp_path / 'target'
    with SessionDB(db_path=source / 'state.db') as db:
        db.create_session('parent', source='cli')
        store = Store(source)
        ensure_import(store, 'parent', db)
        row = accept(db, store, 'Original primary', 'first')
        original_id = store.contract('parent')['id']
        accept(db, store, 'Later supplement', 'late')
        exported = store.export_legacy('parent')
    restored = Store(target)
    restored.import_legacy('parent', exported)
    with SessionDB(db_path=target / 'state.db') as db:
        db.create_session('parent', source='cli')
        fork(db, target, [row])
        ensure_import(restored, 'branch', db)
        assert restored.contract('branch')['id'] == original_id
        assert restored.contract('branch')['raw_supplements'] == []
        branch_export = json.loads(restored.export_legacy('branch'))
        assert branch_export['task_context_export']['branch_boundary']['through_occurrence_id'] == 'first'
        copy = Store(tmp_path / 'third')
        copy.import_legacy('branch', json.dumps(branch_export))
        with copy.connection() as conn:
            receipt = conn.execute('SELECT source FROM branch_boundaries WHERE session_id=?', ('branch',)).fetchone()[0]
        assert receipt == 'copied_message_uids'


def test_identity_missing_or_unmatched_never_discards_unresolved_state(tmp_path):
    from hermes_state import SessionDB
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('parent', source='cli')
        store = Store(tmp_path)
        ensure_import(store, 'parent', db)
        row = accept(db, store, 'Primary', 'first', link=False)
        # A real copied UID without any canonical link cannot prove a cutoff.
        fork(db, tmp_path, [row], new='unmatched')
        result = read_task_contract(session_id='unmatched', session_db=db, hermes_home=tmp_path)
        assert result['status'] == 'error'
        assert not store.imported('unmatched')
        assert store.contract('unmatched') is None
        assert store.contract('parent')['raw_primary_text'] == 'Primary'
        # A legacy/empty branch with no identities retains state, records the
        # ambiguity, and makes no claim that it recovered a historical cutoff.
        db.create_session('legacy-branch', source='cli', parent_session_id='parent',
                          model_config={'_branched_from': 'parent'})
        ensure_import(store, 'legacy-branch', db)
        assert store.contract('legacy-branch')['raw_primary_text'] == 'Primary'
        with store.connection() as conn:
            receipt = conn.execute('SELECT source FROM branch_boundaries WHERE session_id=?', ('legacy-branch',)).fetchone()[0]
        assert receipt == 'no_identity_conservative'


def test_pending_boundary_has_no_receipt_until_parent_publication(tmp_path):
    from hermes_state import SessionDB
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('parent', source='cli')
        store = Store(tmp_path)
        ensure_import(store, 'parent', db)
        accept(db, store, 'Primary', 'first')
        pending = record('Pending supplement', 'pending')
        store.admit('parent', [pending])
        db.append_message('parent', role='user', content=pending['text'])
        row = db.get_messages_as_conversation('parent')[-1]
        store.link('parent', [pending], row['message_uid'])
        fork(db, tmp_path, [row])
        failed = read_task_contract(session_id='branch', session_db=db, hermes_home=tmp_path)
        assert failed['status'] == 'error'
        assert not store.imported('branch')
        with store.connection() as conn:
            assert conn.execute('SELECT 1 FROM branch_boundaries WHERE session_id=?', ('branch',)).fetchone() is None
        store.drain('parent', lambda s, r: decision('supplement', 'append_contract', r['text']))
        accept(db, store, 'Not part of branch', 'late')
        ensure_import(store, 'branch', db)
        assert store.contract('branch')['raw_supplements'] == ['Pending supplement']
        store.drain('branch', lambda *_: pytest.fail('copied pending receipt reran classifier'))


@pytest.mark.parametrize('copy_metadata', [False, True])
def test_later_merged_uid_needs_copied_constituent_identity(tmp_path, copy_metadata):
    from hermes_state import SessionDB
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('parent', source='cli')
        store = Store(tmp_path)
        ensure_import(store, 'parent', db)
        original = accept(db, store, 'Primary', 'first')
        fork(db, tmp_path, [original], copy_metadata=copy_metadata)
        accept(db, store, 'Late merged supplement', 'late')
        # A native merged/revised row can retain the first constituent's UID.
        # The copied UID alone cannot reveal which version the branch copied.
        store.link('parent', [record('Late merged supplement', 'late')], original['message_uid'])
        result = read_task_contract(session_id='branch', session_db=db, hermes_home=tmp_path)
        if copy_metadata:
            assert result['status'] == 'ok'
            assert result['contract']['raw_supplements'] == []
        else:
            assert result['status'] == 'error'
            assert not store.imported('branch')
            assert store.contract('branch') is None
        assert store.contract('parent')['raw_supplements'] == ['Late merged supplement']


def test_explicit_branch_lifecycle_uses_same_uid_cutoff(tmp_path):
    from hermes_state import SessionDB
    from task_context.engine import TaskContextEngine
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('parent', source='cli')
        store = Store(tmp_path)
        ensure_import(store, 'parent', db)
        row = accept(db, store, 'Primary', 'first')
        fork(db, tmp_path, [row])
        accept(db, store, 'Late parent supplement', 'late')
        obj = TaskContextEngine()
        obj.configure_compressor(model='test', config_context_length=32000, quiet_mode=True)
        obj.on_session_start('branch', hermes_home=str(tmp_path), agent_context='primary',
                             boundary_reason='branch', old_session_id='parent', session_db=db)
        assert obj._contract()['raw_supplements'] == []
