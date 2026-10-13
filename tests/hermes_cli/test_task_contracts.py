from dataclasses import asdict

import pytest

from hermes_cli.task_contracts import TaskContractUnavailable, load_task_contract


def test_no_provider_is_feature_off(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: False)
    assert load_task_contract("s") is None


@pytest.mark.parametrize("result", [[], [{"status": "error", "error": "database unavailable"}], [{"status": "ok", "contract": {"id": 3}}]])
def test_failed_provider_is_not_empty_contract(monkeypatch, result):
    monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: True)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *a, **kw: result)
    with pytest.raises(TaskContractUnavailable):
        load_task_contract("s")


@pytest.mark.parametrize("result", [[{"status": "disabled"}], [{"status": "ok", "contract": None}]])
def test_disabled_or_successfully_empty_provider(monkeypatch, result):
    monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: True)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *a, **kw: result)
    assert load_task_contract("s") is None


def test_provider_dto_and_explicit_home_scope(monkeypatch, tmp_path):
    from hermes_constants import get_hermes_home
    from types import SimpleNamespace
    homes = []
    contract = {"id": "preserved-id", "status": "active", "raw_primary_text": "  Ω\nexact  ", "raw_supplements": [" supplement "]}
    monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: True)
    def invoke(*a, **kw):
        homes.append(get_hermes_home())
        return [{"status": "ok", "contract": contract}]
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", invoke)
    before = get_hermes_home()
    for name in ("a", "b", "a"):
        path = tmp_path / name
        result = load_task_contract("s", db=SimpleNamespace(db_path=path / "state.db"))
        assert result.id == contract["id"]
        assert result.task_contract.raw_primary_text == contract["raw_primary_text"]
        assert result.task_contract.raw_supplements == contract["raw_supplements"]
        assert get_hermes_home() == before
    assert homes == [tmp_path / n for n in ("a", "b", "a")]
