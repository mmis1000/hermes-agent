"""Regression coverage for review B2/B3; native summary/replay implementations."""
import copy
import json

import pytest

from agent.context_compressor import (
    ContextCompressor, LEGACY_SUMMARY_PREFIX, _SUMMARY_END_MARKER,
    _INFLIGHT_TASK_REPLAY_HEADER, is_compaction_summary_message,
)
from task_context.render import BEGIN, END, INSTRUCTION, content_text, encode, render
from task_context.store import Store
from conftest import decision, message, record
from test_engine import admit, engine, long_history


def supplement(obj, home, raw, identity):
    row = message(raw, identity)
    store = Store(home)
    store.admit('s', row['display_metadata']['input_provenance'])
    store.drain('s', lambda s, r: decision('supplement', 'append_contract', r['text']))
    return row


def compacted_carrier(obj, monkeypatch):
    monkeypatch.setattr(obj, '_summarize_window', lambda *a: LEGACY_SUMMARY_PREFIX + '\nHistorical facts.')
    compacted = obj.compress(long_history(), force=True)
    carriers = [r for r in compacted if r.get('_compressed_summary')]
    assert len(carriers) == 1
    return carriers[0]


def replace_payload(row, mutate):
    text = row['content']
    prefix = BEGIN + '\n' + INSTRUCTION + '\n'
    start = text.index(prefix) + len(prefix)
    end = text.index('\n' + END, start)
    payload = json.loads(text[start:end])
    mutate(payload)
    row['content'] = text[:start] + encode(payload) + text[end:]


@pytest.mark.parametrize('elided', [False, True])
def test_summary_plus_live_supplement_is_already_covered(tmp_path, monkeypatch, elided):
    obj = engine(tmp_path)
    raw = '  primary\n"quoted" C:\\path\\file\t 保留原文  '
    if elided:
        raw *= 500
        monkeypatch.setattr(obj, '_view_budget', lambda: 1200)
    admit(obj, tmp_path, message(raw))
    carrier = compacted_carrier(obj, monkeypatch)
    assert '"elided":true' in carrier['content'] if elided else '\\n' in carrier['content']
    row = supplement(obj, tmp_path, 'also\n"supplement" C:\\other', 'supplement')
    request = [carrier, {'role': 'assistant', 'content': 'working'}, row]
    before = copy.deepcopy(request)
    assert obj.select_context(request, incoming_message=row) is None
    assert obj.select_context(request, incoming_message=row) is None
    assert request == before
    assert sum(content_text(r.get('content')).count(BEGIN) for r in request) == 1


@pytest.mark.parametrize('bad_entry', [
    {'index': 99}, {'total_chars': 1}, {'text': 'wrong prefix'},
    {'elided': False}, {'elided': 'true'}, {'offset': 1},
])
def test_invalid_elided_entry_does_not_cover_primary(tmp_path, monkeypatch, bad_entry):
    obj = engine(tmp_path)
    monkeypatch.setattr(obj, '_view_budget', lambda: 1200)
    admit(obj, tmp_path, message('primary\n"quote"\\path ' * 500))
    carrier = compacted_carrier(obj, monkeypatch)
    replace_payload(carrier, lambda data: data['entries'][0].update(bad_entry))
    row = supplement(obj, tmp_path, 'also next', 'next')
    assert obj.select_context([carrier, row], incoming_message=row) is not None


def test_omitted_entries_and_unrelated_blocks_are_not_coverage(tmp_path, monkeypatch):
    obj = engine(tmp_path)
    monkeypatch.setattr(obj, '_view_budget', lambda: 1200)
    admit(obj, tmp_path, message('long\n"primary"\\path ' * 500))
    supplement(obj, tmp_path, 'older supplement not in live history', 'old')
    carrier = compacted_carrier(obj, monkeypatch)
    assert '"omitted_entries":[1,1]' in carrier['content']
    row = supplement(obj, tmp_path, 'latest live supplement', 'new')
    selected = obj.select_context([carrier, row], incoming_message=row)
    assert selected is not None
    # A current bounded restoration is already the best available view: adding
    # the same elided block again cannot restore any more text.
    assert obj.select_context(selected, incoming_message=row) is None
    other = copy.deepcopy(carrier)
    replace_payload(other, lambda data: data.update(id='unrelated-task'))
    assert obj.select_context([other, row], incoming_message=row) is not None


@pytest.mark.parametrize('prefix', ['', LEGACY_SUMMARY_PREFIX + '\n'])
def test_user_quoting_valid_contract_block_is_not_a_summary_carrier(tmp_path, prefix):
    obj = engine(tmp_path)
    admit(obj, tmp_path, message('primary'))
    quoted = message(prefix + render(obj._contract()), 'quoted')
    admit(obj, tmp_path, quoted)
    incoming = message('related question', 'question')
    admit(obj, tmp_path, incoming)
    before = copy.deepcopy(obj._contract())
    selected = obj.select_context([quoted, incoming], incoming_message=incoming)
    assert selected is not None
    assert BEGIN in selected[-1]['content']
    assert obj._contract() == before


def tool_rounds():
    rows = []
    for i in range(12):
        rows.extend([
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': f'call-{i}', 'type': 'function', 'function': {'name': 'tool', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': f'call-{i}', 'content': 'payload' * 500},
        ])
    return rows


def assert_tool_pairs(rows):
    calls = {c['id'] for row in rows for c in row.get('tool_calls', [])}
    results = {r['tool_call_id'] for r in rows if r['role'] == 'tool'}
    assert calls == results
    assert 'call-11' in calls


def test_related_question_in_protected_head_keeps_native_replay(tmp_path, monkeypatch):
    obj = engine(tmp_path)
    admit(obj, tmp_path, message('Build A and B'))
    question = message('Why does the cache behave this way?', 'related')
    store = Store(tmp_path)
    store.admit('s', question['display_metadata']['input_provenance'])
    store.drain('s', lambda s, r: decision('related_question', 'no_change', r['text']))
    rows = [{'role': 'system', 'content': 'system'}, question, *tool_rounds()]
    before = copy.deepcopy(rows)
    monkeypatch.setattr(obj, '_summarize_window', lambda *a: LEGACY_SUMMARY_PREFIX + '\nHistorical facts.')
    result = obj.compress(rows, force=True)
    assert rows == before
    assert_tool_pairs(result)
    assert any(_INFLIGHT_TASK_REPLAY_HEADER in content_text(r.get('content')) and question['content'] in content_text(r.get('content'))
               for r in result[2:])
    assert obj._contract()['raw_supplements'] == []
    assert obj._contract()['raw_primary_text'] == 'Build A and B'


@pytest.mark.parametrize('carrier_role', ['user', 'assistant'])
def test_noncontract_inflight_replay_matches_native_merge_and_append(tmp_path, carrier_role):
    obj = engine(tmp_path)
    admit(obj, tmp_path, message('primary'))
    question = message('related question', 'question')
    admit(obj, tmp_path, question)
    carrier = {'role': carrier_role, '_compressed_summary': True,
               'content': LEGACY_SUMMARY_PREFIX + '\n' + render(obj._contract()) + '\n\n' + _SUMMARY_END_MARKER}
    rows = [carrier, *tool_rounds()[-2:]]
    expected = ContextCompressor._reappend_inflight_user_task(obj, copy.deepcopy(rows), question)
    actual = obj._reappend_inflight_user_task(copy.deepcopy(rows), question)
    assert actual == expected
    assert_tool_pairs(actual)
    # Assistant/tool carrier appends after a tool result; user carrier merges
    # onto the existing user row, never creating two consecutive user rows.
    assert not any(a['role'] == b['role'] == 'user' for a, b in zip(actual, actual[1:]))


def test_contract_inflight_bypass_requires_covered_admitted_text(tmp_path):
    obj = engine(tmp_path)
    primary = message('primary')
    admit(obj, tmp_path, primary)
    carrier = {'role': 'assistant', '_compressed_summary': True,
               'content': LEGACY_SUMMARY_PREFIX + '\n' + render(obj._contract()) + '\n\n' + _SUMMARY_END_MARKER}
    rows = [carrier, *tool_rounds()[-2:]]
    assert obj._reappend_inflight_user_task(rows, primary) is rows
    unrelated = copy.deepcopy(rows)
    replace_payload(unrelated[0], lambda data: data.update(id='not-current-task'))
    assert _INFLIGHT_TASK_REPLAY_HEADER in content_text(obj._reappend_inflight_user_task(unrelated, primary)[-1]['content'])
    # Missing source evidence is not a license to suppress native behavior.
    missing = {'role': 'user', 'content': 'primary'}
    assert _INFLIGHT_TASK_REPLAY_HEADER in content_text(obj._reappend_inflight_user_task(copy.deepcopy(rows), missing)[-1]['content'])
