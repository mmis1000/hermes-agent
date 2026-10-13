import json
import sqlite3

import pytest

from task_context.migration import ensure_import, read_task_contract
from task_context.store import Store
from conftest import record, decision


def legacy_raw(status='active'):
    return json.dumps(dict(id='legacy-id', created_at=1, updated_at=2, status=status,
                           task_contract=dict(raw_primary_text='  原文\n', raw_supplements=[' old  '] * 30),
                           session_lineage=['parent-' + str(i) for i in range(15)],
                           raw_messages=[dict(id='raw-' + str(i), raw_text='x', source_kind='direct_user') for i in range(50)]))


class DB:
    def __init__(self, raw):
        self.raw = raw
        self.calls = 0
    def get_meta(self, key):
        self.calls += 1
        return self.raw


@pytest.mark.parametrize('status', ['active', 'cancelled', 'superseded'])
def test_import_idempotent_before_provider_read_and_preserves_fields(tmp_path, status):
    db = DB(legacy_raw(status))
    first = read_task_contract(session_id='s', session_db=db, hermes_home=tmp_path)
    second = read_task_contract(session_id='s', session_db=db, hermes_home=tmp_path)
    assert first == second
    assert db.calls == 1
    assert first['contract']['id'] == 'legacy-id'
    assert first['contract']['status'] == status
    assert first['contract']['raw_primary_text'] == '  原文\n'
    assert len(first['contract']['raw_supplements']) == 30
    assert len(first['contract']['session_lineage']) == 15
    assert len(Store(tmp_path).snapshot('s')[1][0].raw_messages) == 50


def test_crash_before_receipt_rolls_back_then_retry(tmp_path):
    store = Store(tmp_path)
    def crash():
        raise RuntimeError('power loss')
    with pytest.raises(RuntimeError):
        store.import_legacy('s', legacy_raw(), before_receipt=crash)
    assert store.contract('s') is None
    assert not store.imported('s')
    assert store.import_legacy('s', legacy_raw())
    assert store.contract('s')['id'] == 'legacy-id'


def test_provider_failure_is_not_empty_success(tmp_path):
    result = read_task_contract(session_id='s', session_db=DB('broken JSON'), hermes_home=tmp_path)
    assert result['status'] == 'error'
    assert 'contract' not in result
    assert not Store(tmp_path).imported('s')
    result = read_task_contract(session_id='s', session_db=DB(None), hermes_home=tmp_path)
    assert result == {'status': 'ok', 'contract': None}


def test_rollback_export_roundtrip_preserves_all_new_inputs(tmp_path):
    store = Store(tmp_path / 'a')
    store.import_legacy('s', legacy_raw())
    for i in range(40):
        store.admit('s', [record('  add-' + str(i), str(i), platform_text='original ' + str(i))])
        store.drain('s', lambda s, r: decision('supplement', 'append_contract', r['text']))
    exported = store.export_legacy('s')
    compat = json.loads(exported)
    assert len(compat['task_contract']['raw_supplements']) == 70
    restored = Store(tmp_path / 'b')
    restored.import_legacy('s', exported)
    assert restored.contract('s')['id'] == store.contract('s')['id']
    assert restored.contract('s')['raw_supplements'] == store.contract('s')['raw_supplements']
    with restored.connection() as db:
        rows = db.execute('SELECT payload FROM inputs ORDER BY seq').fetchall()
    assert len(rows) == 40
    assert json.loads(rows[-1][0])['platform_text'] == 'original 39'
    restored.admit('s', [record('  add-39', '39', platform_text='original 39')])
    restored.drain('s', lambda *_: pytest.fail('export lost input receipt'))


def test_export_uses_one_snapshot_during_concurrent_publication(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    store = Store(tmp_path / 'source')
    store.admit('s', [record('primary', 'one')])
    store.drain('s', lambda *_: None)
    store.admit('s', [record('new supplement', 'two')])
    reading, publish_ready, release = threading.Event(), threading.Event(), threading.Event()
    view = store._contract_view
    publish = store.publish
    def paused_view(*args):
        reading.set()
        assert release.wait(5)
        return view(*args)
    def observed_publish(*args):
        publish_ready.set()
        return publish(*args)
    monkeypatch.setattr(store, '_contract_view', paused_view)
    monkeypatch.setattr(store, 'publish', observed_publish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        export = pool.submit(store.export_legacy, 's')
        assert reading.wait(5)
        writing = pool.submit(store.drain, 's', lambda s, r: decision('supplement', 'append_contract', r['text']))
        assert publish_ready.wait(5)
        release.set()
        raw = export.result(5)
        writing.result(5)
    data = json.loads(raw)
    assert data['task_contract']['raw_supplements'] == []
    assert data['task_context_export']['tasks'][0]['task_contract']['raw_supplements'] == []
    assert data['task_context_export']['inputs'][-1]['processed'] == 0
    restored = Store(tmp_path / 'restored')
    restored.import_legacy('s', raw)
    restored.drain('s', lambda s, r: decision('supplement', 'append_contract', r['text']))
    assert restored.contract('s')['raw_supplements'] == ['new supplement']


@pytest.mark.parametrize('parent_end_reason', ['branched', 'compression'])
def test_real_sessiondb_branch_marker_copies_independently_on_native_start(tmp_path, parent_end_reason):
    from hermes_state import SessionDB
    from task_context.engine import TaskContextEngine
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        db.create_session('root', source='cli')
        db.set_meta('task_intent:root', legacy_raw())
        db.end_session('root', parent_end_reason)
        store = Store(tmp_path)
        ensure_import(store, 'root', db)
        shared = record('accepted before branch', 'accepted')
        store.admit('root', [shared])
        store.drain('root', lambda s, r: decision('supplement', 'append_contract', r['text']))
        # Actual /branch producer shape, without a lifecycle boundary_reason.
        db.create_session('branch', source='cli', parent_session_id='root',
                          model_config={'_branched_from': 'root'})
        obj = TaskContextEngine()
        obj.configure_compressor(model='test', config_context_length=32000, quiet_mode=True)
        obj.on_session_start('branch', hermes_home=str(tmp_path), session_db=db,
                             agent_context='primary', platform='cli')
        assert obj._contract()['id'] == 'legacy-id'
        assert store.owner('branch') == 'branch'
        assert obj._contract()['session_lineage'][-1] == 'root'
        store.admit('branch', [shared])
        store.drain('branch', lambda *_: pytest.fail('copied branch occurrence was replayed'))
        store.admit('branch', [record('branch only', 'branch-new')])
        store.drain('branch', lambda s, r: decision('supplement', 'append_contract', r['text']))
        assert store.contract('branch')['raw_supplements'][-1] == 'branch only'
        assert store.contract('root')['raw_supplements'][-1] == 'accepted before branch'
        snapshot = store.contract('branch')
        ensure_import(store, 'branch', db)
        assert store.contract('branch') == snapshot
        assert db.get_meta('task_intent:branch') is None
    finally:
        db.close()


def test_real_sessiondb_inherited_branch_marker_is_compression_alias(tmp_path):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        db.create_session('root', source='cli')
        db.set_meta('task_intent:root', legacy_raw())
        db.create_session('branch', source='cli', parent_session_id='root',
                          model_config={'_branched_from': 'root'})
        db.end_session('branch', 'compression')
        db.create_session('tip', source='cli', parent_session_id='branch',
                          model_config={'_branched_from': 'root'})
        store = Store(tmp_path)
        ensure_import(store, 'tip', db)
        assert store.owner('tip') == 'branch'
        assert store.owner('root') == 'root'
        assert store.contract('tip')['session_lineage'][-2:] == ['root', 'branch']
        store.admit('tip', [record('after compression', 'tip-input')])
        store.drain('tip', lambda s, r: decision('supplement', 'append_contract', r['text']))
        assert store.contract('branch')['raw_supplements'][-1] == 'after compression'
        assert store.contract('root')['raw_supplements'][-1] == ' old  '
    finally:
        db.close()


@pytest.mark.parametrize('source,config,parent_end_reason', [
    ('cli', {}, None),
    ('cli', {'_branched_from': 'ancestor'}, None),
    ('cli', {'_delegate_from': 'root'}, 'compression'),
    ('delegate', {'_delegate_from': 'root'}, 'compression'),
    ('subagent', {'_branched_from': 'root'}, 'compression'),
    ('cli', {'_branched_from': 'root', '_delegate_from': 'root'}, 'compression'),
])
def test_real_sessiondb_parent_link_does_not_promote_default_or_delegation(tmp_path, source, config, parent_end_reason):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        db.create_session('root', source='cli')
        db.set_meta('task_intent:root', legacy_raw())
        if parent_end_reason:
            db.end_session('root', parent_end_reason)
        db.create_session('child', source=source, parent_session_id='root', model_config=config)
        result = read_task_contract(session_id='child', session_db=db, hermes_home=tmp_path)
        assert result == {'status': 'ok', 'contract': None}
        assert Store(tmp_path).owner('child') == 'child'
        assert db.get_meta('task_intent:child') is None
    finally:
        db.close()


def test_persisted_compression_parent_migrates_read_only(tmp_path):
    path = tmp_path / 'state.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE state_meta(key TEXT PRIMARY KEY,value TEXT)')
        db.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,parent_session_id TEXT,source TEXT,end_reason TEXT,model_config TEXT)')
        db.execute('INSERT INTO state_meta VALUES(?,?)', ('task_intent:parent', legacy_raw()))
        db.executemany('INSERT INTO sessions(id,parent_session_id,source,end_reason) VALUES(?,?,?,?)', [
            ('parent', None, 'discord', 'compression'), ('child', 'parent', 'discord', None),
            ('delegated', 'parent', 'subagent', None)])
    before = path.read_bytes()
    store = Store(tmp_path)
    ensure_import(store, 'child')
    assert store.contract('child')['id'] == 'legacy-id'
    assert 'parent' in store.contract('child')['session_lineage']
    ensure_import(store, 'delegated')
    assert store.contract('delegated') is None
    assert path.read_bytes() == before
