import sys
from pathlib import Path

# The host conftest owns collection-time isolation. Only tests need a direct
# import path; production loads this package through the plugin manager.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'plugins' / 'task-context'))

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    import hermes_state
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', tmp_path / 'state.db')
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(tmp_path)
    yield tmp_path
    reset_hermes_home_override(token)


def record(text, occurrence='one', kind='human', **kwargs):
    return dict(occurrence_id=occurrence, text=text, source_kind=kind,
                source_id=kwargs.pop('source_id', 'alice'), platform=kwargs.pop('platform', 'cli'),
                message_id=kwargs.pop('message_id', occurrence), **kwargs)


def message(text, occurrence='one', kind='human', **kwargs):
    return {'role': 'user', 'content': text, 'display_metadata': {
        'input_provenance': [record(text, occurrence, kind, **kwargs)]}}


def decision(relationship, effect, text, confidence=1):
    return dict(relationship=relationship, state_effect=effect, confidence=confidence,
                evidence_quotes=[text], reason_codes=['fixture'])
