import copy
import json

import pytest

from agent.context_compressor import LEGACY_SUMMARY_PREFIX, is_compaction_summary_message
from task_context.ingress import admitted_records, classify_with_deadline
from task_context.judge import TaskIntentMicroJudgeConfig
from task_context.render import BEGIN, content_text, render
from task_context.store import Store
from conftest import record, message, decision
from test_engine import engine, admit, long_history


def test_cross_profile_engine_reuse_rejected_before_import(tmp_path):
    obj = engine(tmp_path / 'a')
    admit(obj, tmp_path / 'a', message('private A'))
    with pytest.raises(ValueError, match='profile'):
        admit(obj, tmp_path / 'b', message('private B'))
    assert not (tmp_path / 'b' / 'task-context').exists()
    assert obj._contract()['raw_primary_text'] == 'private A'


@pytest.mark.parametrize('platform', ['discord', 'telegram', 'slack', 'cli', 'matrix', 'whatsapp'])
def test_all_chat_sources_use_same_admitted_authority(tmp_path, platform):
    obj = engine(tmp_path)
    row = message('exact input', platform=platform, source_id='group/user-one')
    row['content'] = 'decorated [group/user-one]: exact input'
    admit(obj, tmp_path, row)
    assert obj._contract()['raw_primary_text'] == 'exact input'
    assert admitted_records(row)[0]['source_id'] == 'group/user-one'


def test_empty_attachment_input_is_not_a_task(tmp_path):
    obj = engine(tmp_path)
    admit(obj, tmp_path, message(''))
    assert obj._contract() is None
    assert Store(tmp_path).pending('s') is None


def test_ordinary_summary_quote_is_not_an_active_contract(tmp_path):
    obj = engine(tmp_path)
    admit(obj, tmp_path, message('A and B'))
    incoming = message('question', 'two')
    admit(obj, tmp_path, incoming)
    request = [{'role': 'user', 'content': LEGACY_SUMMARY_PREFIX + '\nA and B were already addressed.'},
               {'role': 'assistant', 'content': 'old answer'}, incoming]
    result = obj.select_context(request, incoming_message=incoming)
    assert result is not None
    assert BEGIN in result[-1]['content']


def test_cancelled_contract_block_is_removed_from_new_summary(tmp_path, monkeypatch):
    obj = engine(tmp_path)
    admit(obj, tmp_path, message('A and B'))
    old = render(obj._contract())
    store = Store(tmp_path)
    store.admit('s', [record('stop', 'two')])
    store.drain('s', lambda s, r: decision('cancellation', 'cancel', r['text']))
    monkeypatch.setattr(obj, '_summarize_window', lambda *a: LEGACY_SUMMARY_PREFIX + '\nOld material\n' + old)
    result = obj.compress(long_history(), force=True)
    assert all(BEGIN not in content_text(row.get('content')) for row in result)
    assert obj._contract()['status'] == 'cancelled'


def test_compression_keeps_tool_pairs_and_avoids_synthetic_replay(tmp_path, monkeypatch):
    obj = engine(tmp_path)
    admit(obj, tmp_path, message('Do A and B'))
    rows = [{'role': 'system', 'content': 'system'}, message('Do A and B')]
    for i in range(12):
        rows.extend([
            {'role': 'assistant', 'content': None,
             'tool_calls': [{'id': f'call-{i}', 'type': 'function', 'function': {'name': 'tool', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': f'call-{i}', 'content': 'payload' * 500}])
    snapshot = copy.deepcopy(rows)
    monkeypatch.setattr(obj, '_summarize_window', lambda *a: LEGACY_SUMMARY_PREFIX + '\nHistorical facts.')
    result = obj.compress(rows, force=True)
    assert rows == snapshot
    calls = {c['id'] for r in result for c in r.get('tool_calls', [])}
    results = {r['tool_call_id'] for r in result if r['role'] == 'tool'}
    assert calls == results
    assert 'call-11' in calls
    assert sum(BEGIN in content_text(r.get('content')) for r in result) == 1
    # Only original input and native summary carrier may carry a user role.
    assert all(r.get('content') == 'Do A and B' or is_compaction_summary_message(r)
               for r in result if r.get('role') == 'user')


def test_auxiliary_route_and_profile_context_are_explicit(tmp_path, monkeypatch):
    obj = engine(tmp_path)
    admit(obj, tmp_path, message('A and B'))
    state = Store(tmp_path).snapshot('s')[1][0]
    observed = {}
    def fake(**kwargs):
        from hermes_constants import get_hermes_home
        observed.update(kwargs)
        observed['home'] = str(get_hermes_home())
        return json.dumps(decision('supplement', 'append_contract', 'also C'))
    import agent.auxiliary_client
    monkeypatch.setattr(agent.auxiliary_client, 'call_llm', fake)
    runtime = dict(model='effective', provider='provider', base_url='https://example.invalid',
                   api_key='test-only', api_mode='chat_completions')
    result = classify_with_deadline(state, record('also C', 'two'), home=tmp_path,
                                    runtime=runtime, config=TaskIntentMicroJudgeConfig())
    assert result.state_effect == 'append_contract'
    assert observed['task'] == 'task_intent'
    assert observed['main_runtime'] == runtime
    assert observed['home'] == str(tmp_path)
    assert observed['temperature'] == 0
    assert 'tools' not in observed
