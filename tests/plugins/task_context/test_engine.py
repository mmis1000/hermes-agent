import copy
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

from agent.context_compressor import ContextCompressor, LEGACY_SUMMARY_PREFIX, is_compaction_summary_message
from task_context.engine import TaskContextEngine
from task_context.ingress import admitted_records, classify_with_deadline
from task_context.judge import TaskIntentMicroJudgeConfig
from task_context.render import BEGIN, content_text, render
from task_context.store import Store
from conftest import record, message, decision


def engine(home, judge=None, role='primary', session='s', **overrides):
    obj = TaskContextEngine(judge_call=judge)
    kwargs = dict(model='test', config_context_length=32000, quiet_mode=True,
                  protect_first_n=3, protect_last_n=3, threshold_percent=0.6,
                  max_tokens=1234, abort_on_summary_failure=True)
    kwargs.update(overrides)
    obj.configure_compressor(**kwargs)
    obj.on_session_start(session, hermes_home=str(home), agent_context=role, platform='cli')
    return obj


def admit(obj, home, row, role='primary', session='s'):
    obj.on_input(row, session_id=session, hermes_home=str(home), agent_context=role, platform='cli')


def supplement_judge(**kwargs):
    data = json.loads(kwargs['messages'][1]['content'])
    return json.dumps(decision('supplement', 'append_contract', data['current_message']['text']))


def test_native_configuration_parity_and_clone_isolation(tmp_path):
    kwargs = dict(model='x', threshold_percent=0.66, protect_first_n=5, protect_last_n=17,
                  summary_target_ratio=0.35, quiet_mode=True, summary_model_override='summary',
                  base_url='https://example.invalid', api_key='test-only', config_context_length=40000,
                  provider='custom', api_mode='chat_completions', abort_on_summary_failure=True,
                  max_tokens=3456, model_thresholds={'x': 0.7}, threshold_tokens_cap=23000,
                  proactive_prune_tokens=12000, proactive_prune_min_result_chars=345,
                  proactive_prune_min_reclaim_tokens=123, min_tail_user_messages=2, tail_mode='legacy',
                  custom_providers=[{'name': 'custom', 'base_url': 'https://example.invalid'}])
    native, obj = ContextCompressor(**kwargs), TaskContextEngine()
    obj.configure_compressor(**kwargs)
    for key, value in vars(native).items():
        assert getattr(obj, key) == value, key
    assert obj.threshold_tokens == native.threshold_tokens
    assert obj.tail_token_budget == native.tail_token_budget
    obj._session_db = threading.Lock()  # deliberately uncopyable
    clone = obj.clone_for_agent()
    assert not clone._task_configured
    assert clone._session_db is None
    clone.configure_compressor(model='child', config_context_length=8192, quiet_mode=True)
    assert obj.context_length == 40000
    assert clone.context_length == 8192


def test_ordered_batch_exact_adapter_platform_original_and_retry(tmp_path):
    (tmp_path / 'config.yaml').write_text('task_intents:\n  relationship_judge:\n    enabled: true\n')
    calls = []
    def judge(**kwargs):
        calls.append(kwargs)
        return supplement_judge(**kwargs)
    obj = engine(tmp_path, judge)
    row = message('decorated model text')
    row['display_metadata']['input_provenance'] = [
        record('  raw 原文\n', 'a', platform_text='  platform 原文\n', source_id='alice'),
        record('also B', 'b', source_id='bob', platform='discord')]
    admit(obj, tmp_path, row)
    admit(obj, tmp_path, row)
    assert len(calls) == 1
    contract = obj._contract()
    assert contract['raw_primary_text'] == '  raw 原文\n'
    assert contract['raw_supplements'] == ['also B']
    store = Store(tmp_path)
    with store.connection() as db:
        payloads = [json.loads(r[0]) for r in db.execute('SELECT payload FROM inputs ORDER BY seq')]
    assert payloads[0]['platform_text'] == '  platform 原文\n'
    assert payloads[1]['source_id'] == 'bob'
    row['message_uid'] = 'persistent-host-id'
    obj.on_turn_complete([row])
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM links').fetchone()[0] == 2


@pytest.mark.parametrize('role', ['subagent', 'cron', 'flush', 'automation', 'unknown'])
def test_nonprimary_never_writes_human_store(tmp_path, role):
    obj = engine(tmp_path, role=role)
    admit(obj, tmp_path, message('child instruction'), role=role)
    assert not (tmp_path / 'task-context').exists()
    assert obj.select_context([message('x')]) is None
    assert obj.compress([message('short')]) == [message('short')]


def test_untrusted_history_and_machine_input_not_promoted(tmp_path):
    obj = engine(tmp_path)
    assert admitted_records({'role': 'user', 'content': 'unattributed old history'}) == []
    admit(obj, tmp_path, {'role': 'user', 'content': 'unattributed old history'})
    assert obj._contract() is None
    admit(obj, tmp_path, message('machine says do things', kind='machine'))
    assert obj._contract() is None
    human = message('typed steer', occurrence='steer')
    human['display_kind'] = 'steer'
    admit(obj, tmp_path, human)
    assert obj._contract()['raw_primary_text'] == 'typed steer'


def test_model_switch_and_rotation_keep_contract_new_id_does_not(tmp_path):
    obj = engine(tmp_path)
    admit(obj, tmp_path, message('A and B'))
    identity = obj._contract()['id']
    obj.update_model('new-model', 64000)
    obj.on_session_reset()
    assert obj._contract()['id'] == identity
    obj.on_session_start('rotated', hermes_home=str(tmp_path), agent_context='primary',
                         boundary_reason='compression', old_session_id='s')
    assert obj._contract()['id'] == identity
    assert obj._contract()['session_lineage'] == ['s']
    obj.on_session_start('new', hermes_home=str(tmp_path), agent_context='primary', boundary_reason='new')
    assert obj._contract() is None
    restarted = engine(tmp_path, session='rotated')
    assert restarted._contract()['id'] == identity


def test_noop_request_and_restore_is_exact_stable_current_row_only(tmp_path):
    obj = engine(tmp_path)
    original = message('A and B')
    admit(obj, tmp_path, original)
    full = [{'role': 'system', 'content': 'system bytes'}, original]
    before = copy.deepcopy(full)
    assert obj.select_context(full, incoming_message=original) is None
    assert full == before
    now = message('related question', 'two')
    admit(obj, tmp_path, now)
    absent = [full[0], {'role': 'assistant', 'content': 'old answer'}, now]
    saved = copy.deepcopy(absent)
    first = obj.select_context(absent, incoming_message=now)
    second = obj.select_context(absent, incoming_message=now)
    assert first == second
    assert absent == saved
    assert len(first) == len(absent)
    assert first[0] is absent[0]
    assert first[1] is absent[1]
    assert BEGIN in first[-1]['content']
    assert obj.select_context(first, incoming_message=now) is None
    assert obj.select_context(absent, incoming_message={'role': 'user', 'content': 'not identified'}) is None


def long_history():
    rows = [{'role': 'system', 'content': 'system'}]
    for i in range(18):
        rows.extend([{'role': 'user', 'content': 'user-' + str(i) + 'x' * 500},
                     {'role': 'assistant', 'content': 'answer-' + str(i) + 'y' * 600}])
    return rows


def test_real_native_compression_carrier_budget_counters_abort_and_no_store_writes(tmp_path, monkeypatch):
    obj = engine(tmp_path)
    admit(obj, tmp_path, message('Build A and B'))
    store = Store(tmp_path)
    snapshot = store.path.read_bytes()
    rows = long_history()
    original = copy.deepcopy(rows)
    monkeypatch.setattr(obj, '_summarize_window', lambda *a: LEGACY_SUMMARY_PREFIX + '\nHistorical work.')
    result = obj.compress(rows, force=True)
    assert obj.compression_count == 1
    carriers = [m for m in result if is_compaction_summary_message(m)]
    assert len(carriers) == 1
    assert BEGIN in content_text(carriers[0]['content'])
    assert 'Build A and B' in content_text(carriers[0]['content'])
    assert result[0]['content'] == 'system'
    assert rows == original
    assert store.path.read_bytes() == snapshot
    assert obj._last_compression_made_progress
    # Repeat compression through the native implementation; exactly one block.
    result += long_history()[1:]
    result2 = obj.compress(result, force=True)
    assert sum(content_text(m.get('content')).count(BEGIN) for m in result2) == 1
    assert obj.compression_count == 2
    monkeypatch.setattr(obj, '_summarize_window', lambda *a: None)
    monkeypatch.setattr(obj, '_abort_on_summary_failure', lambda *a: True)
    untouched = long_history()
    assert obj.compress(untouched, force=True) == untouched
    assert store.path.read_bytes() == snapshot


def test_bounded_deadline_has_no_late_state_publication(tmp_path):
    obj = engine(tmp_path)
    admit(obj, tmp_path, message('A and B'))
    state = Store(tmp_path).snapshot('s')[1][0]
    done, release = threading.Event(), threading.Event()
    def stuck(**kwargs):
        release.wait(5)
        done.set()
        return json.dumps(decision('replacement', 'supersede', 'replace'))
    result = classify_with_deadline(state, record('replace', 'two'), home=tmp_path,
                                    runtime={}, config=TaskIntentMicroJudgeConfig(timeout_seconds=0.05), llm_call=stuck)
    assert result.state_effect == 'no_change'
    assert 'judge_timeout' in result.reason_codes
    before = obj._contract()
    release.set()
    assert done.wait(5)
    assert obj._contract() == before


def test_restoration_tool_returns_exact_slices(tmp_path):
    obj = engine(tmp_path)
    raw = ' 😀\n保留\t' * 5000
    admit(obj, tmp_path, message(raw))
    restored = []
    offset = 0
    while True:
        result = json.loads(obj.handle_tool_call('task_context_read', {'entry': 0, 'offset': offset, 'limit': 5000}))
        assert result['status'] == 'ok'
        restored.append(result['text'])
        if not result['has_more']:
            break
        offset += len(result['text'])
    assert ''.join(restored) == raw


def test_plugin_registration_root(tmp_path):
    path = Path(__file__).resolve().parents[3] / 'plugins' / 'task-context'
    spec = importlib.util.spec_from_file_location('test_task_context_plugin', path / '__init__.py', submodule_search_locations=[str(path)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    class Context:
        def register_context_engine(self, value):
            self.engine = value
        def register_hook(self, name, callback):
            self.name, self.callback = name, callback
    ctx = Context()
    module.register(ctx)
    assert ctx.engine.name == 'task-context'
    assert ctx.name == 'read_task_contract'
    assert ctx.callback(session_id='s', hermes_home=tmp_path) == {'status': 'disabled'}
    assert not (tmp_path / 'task-context').exists()
    (tmp_path / 'config.yaml').write_text('context:\n  engine: task-context\n')
    assert ctx.callback(session_id='s', hermes_home=tmp_path) == {'status': 'ok', 'contract': None}
