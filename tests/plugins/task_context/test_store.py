import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from task_context.policy import TaskRelationshipDecision
from task_context.store import Store
from task_context.render import render
from conftest import record, decision


def seed(home):
    store = Store(home)
    store.admit('s', [record('  α\nBuild A and B\t  ')])
    store.drain('s', lambda *_: pytest.fail('first input must not call judge'))
    return store


def test_raw_and_unbounded_supplements_identity_not_text(tmp_path):
    store = seed(tmp_path)
    for i in range(50):
        store.admit('s', [record('  also 保留\n spacing ', str(i))])
        store.drain('s', lambda s, r: decision('supplement', 'append_contract', r['text']))
    result = store.contract('s')
    assert result['raw_primary_text'] == '  α\nBuild A and B\t  '
    assert result['raw_supplements'] == ['  also 保留\n spacing '] * 50
    states = store.snapshot('s')[1]
    assert len(states[0].raw_messages) == 51
    store.admit('s', [record('  also 保留\n spacing ', '49')], 'host-uid')
    store.drain('s', lambda *_: pytest.fail('retry reclassified'))
    assert len(store.contract('s')['raw_supplements']) == 50
    with store.connection() as db:
        assert db.execute('SELECT message_uid FROM links').fetchone()[0] == 'host-uid'


def test_related_unclear_machine_unknown_do_not_become_obligations(tmp_path):
    store = seed(tmp_path)
    original = store.contract('s')
    for kind in ('machine', 'unknown'):
        store.admit('s', [record('replace everything', kind, kind)])
        store.drain('s', lambda *_: pytest.fail('non-human was classified'))
        assert render(store.contract('s')) == render(original)
    for relationship in ('related_question', 'unclear', 'same_task'):
        store.admit('s', [record(relationship, relationship)])
        store.drain('s', lambda s, r: decision(relationship, 'no_change', r['text']))
    assert store.contract('s')['raw_supplements'] == []
    assert store.contract('s')['raw_primary_text'] == original['raw_primary_text']
    assert len(store.snapshot('s')[1][0].raw_messages) == 4


def test_new_topic_keeps_unresolved_older_task_and_cancel_does_not_erase_it(tmp_path):
    store = seed(tmp_path)
    initial = store.contract('s')['id']
    store.admit('s', [record('Different task', 'two')])
    store.drain('s', lambda s, r: decision('new_task', 'start_new', r['text']))
    assert store.contract('s')['raw_supplements'] == ['  α\nBuild A and B\t  ']
    store.admit('s', [record('Cancel different task', 'three')])
    store.drain('s', lambda s, r: decision('cancellation', 'cancel', r['text']))
    assert store.contract('s')['id'] == initial
    assert store.contract('s')['status'] == 'active'
    assert len(store.snapshot('s')[1]) == 2


def test_replacement_cancel_and_evidence_validation(tmp_path):
    store = seed(tmp_path)
    initial = store.contract('s')['id']
    store.admit('s', [record('Replace it with C', 'two')])
    store.drain('s', lambda s, r: decision('replacement', 'supersede', r['text']))
    assert store.contract('s')['id'] != initial
    assert store.snapshot('s')[1][0].status == 'superseded'
    store.admit('s', [record('Stop C', 'three')])
    store.drain('s', lambda s, r: decision('cancellation', 'cancel', 'not exact'))
    assert store.contract('s')['status'] == 'active'
    store.admit('s', [record('Stop C', 'four')])
    store.drain('s', lambda s, r: decision('cancellation', 'cancel', r['text']))
    assert store.contract('s')['status'] == 'cancelled'


@pytest.mark.parametrize('confidence,quote', [(0.5, 'raw'), (1, 'not present')])
def test_validator_preserves_nonrewriting_gate(confidence, quote):
    d = TaskRelationshipDecision.from_payload(
        dict(relationship='replacement', state_effect='supersede', confidence=confidence,
             evidence_quotes=[quote], rewritten_task='FAKE'), raw_text='raw')
    assert d.state_effect == 'no_change'
    assert 'rewritten_task' not in d.raw_payload


def test_same_identity_conflict_rolls_back_entire_batch(tmp_path):
    store = seed(tmp_path)
    with pytest.raises(ValueError):
        store.admit('s', [record('new', 'later'), record('changed', 'one')])
    assert store.pending('s') is None


def test_concurrent_arrivals_judge_outside_sql_and_once(tmp_path):
    store = seed(tmp_path)
    started, release = threading.Event(), threading.Event()
    calls = []
    def classify(state, rec):
        calls.append(rec['occurrence_id'])
        if rec['occurrence_id'] == 'two':
            started.set()
            assert release.wait(5)
        return decision('supplement', 'append_contract', rec['text'])
    store.admit('s', [record('second', 'two')])
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.drain, 's', classify)
        assert started.wait(5)
        # An actual SQLite write succeeds while the judge is blocked.
        other = Store(tmp_path)
        other.admit('s', [record('third', 'three')])
        second = pool.submit(other.drain, 's', classify)
        release.set()
        first.result(5)
        second.result(5)
    assert calls == ['two', 'three']
    assert store.contract('s')['raw_supplements'] == ['second', 'third']


def test_cas_conflict_retries_oldest_input_without_loss(tmp_path):
    store = seed(tmp_path)
    store.admit('s', [record('supplement', 'two')])
    calls = []
    def classify(state, rec):
        calls.append(state.id)
        if len(calls) == 1:
            with store.connection(write=True) as db:
                db.execute("UPDATE conversations SET version=version+1 WHERE session_id='s'")
        return decision('supplement', 'append_contract', rec['text'])
    store.drain('s', classify)
    assert len(calls) == 2
    assert store.contract('s')['raw_supplements'] == ['supplement']
    assert store.pending('s') is None


def test_fork_replay_isolated_and_new_session_has_no_inheritance(tmp_path):
    store = seed(tmp_path)
    assert store.inherit('s', 'branch', 'branch')
    assert not store.inherit('s', 'new', 'new')
    assert store.contract('new') is None
    store.admit('branch', [record('  α\nBuild A and B\t  ')])
    store.drain('branch', lambda *_: pytest.fail('copied input became command'))
    store.admit('branch', [record('branch only', 'two')])
    store.drain('branch', lambda s, r: decision('supplement', 'append_contract', r['text']))
    assert store.contract('s')['raw_supplements'] == []
    assert store.contract('branch')['raw_supplements'] == ['branch only']
    assert store.contract('branch')['session_lineage'] == ['s']
    assert Store(tmp_path / 'other-profile').contract('s') is None


def test_rotation_during_judge_keeps_one_authority_and_late_result(tmp_path):
    store = seed(tmp_path)
    store.admit('s', [record('late supplement', 'two')])
    started, release = threading.Event(), threading.Event()
    calls = []
    def classify(state, rec):
        calls.append(list(state.session_lineage))
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        return decision('supplement', 'append_contract', rec['text'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(store.drain, 's', classify)
        assert started.wait(5)
        assert store.inherit('s', 'rotated', 'compression')
        # Alias shares authority and occurrence receipt, including a pending input.
        store.admit('rotated', [record('late supplement', 'two')])
        release.set()
        pending.result(5)
    assert calls == [[], ['s']]
    assert store.contract('rotated')['raw_supplements'] == ['late supplement']
    assert store.contract('s') == store.contract('rotated')
    store.drain('rotated', lambda *_: pytest.fail('rotation reapplied transition'))
    assert store.pending('rotated') is None
    assert store.inherit('rotated', 'third', 'compression')
    assert store.contract('third')['session_lineage'] == ['s', 'rotated']


def test_raw_unicode_truncation_restorable_and_bound(tmp_path):
    store = seed(tmp_path)
    store.admit('s', [record('😀\n\t"' * 5000, 'two')])
    store.drain('s', lambda s, r: decision('supplement', 'append_contract', r['text']))
    block = render(store.contract('s'), 1200)
    assert len(block) <= 1200
    assert 'elided' in block or 'omitted_entries' in block
    assert store.contract('s')['raw_supplements'][0] == '😀\n\t"' * 5000
