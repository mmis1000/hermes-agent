from __future__ import annotations


def _run_single_due_job(monkeypatch, tmp_path, final_response: str):
    import cron.scheduler as sched

    job = {"id": "abc123abc123", "name": "silent-marker-regression"}
    delivered: list[str] = []
    marked: list[tuple] = []

    monkeypatch.setattr(sched, "_hermes_home", tmp_path)
    monkeypatch.setattr(sched, "get_due_jobs", lambda: [job])
    monkeypatch.setattr(sched, "advance_next_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        sched,
        "run_job",
        lambda _job: (True, "saved output doc", final_response, None),
    )
    monkeypatch.setattr(sched, "save_job_output", lambda _jid, _output: tmp_path / "out.md")

    def fake_deliver(_job, content, **_kwargs):
        delivered.append(content)
        return None

    def fake_mark(*args, **kwargs):
        marked.append((args, kwargs))

    monkeypatch.setattr(sched, "_deliver_result", fake_deliver)
    monkeypatch.setattr(sched, "mark_job_run", fake_mark)

    count = sched.tick(verbose=False, sync=True)
    return count, delivered, marked


def test_cron_delivers_report_that_mentions_silent_marker(monkeypatch, tmp_path):
    response = "Daily audit report\n\nPrior violation: a job returned [SILENT], but this report should be visible."

    count, delivered, marked = _run_single_due_job(monkeypatch, tmp_path, response)

    assert count == 1
    assert delivered == [response]
    assert marked
    args, kwargs = marked[0]
    assert args[:3] == ("abc123abc123", True, None)
    assert kwargs.get("delivery_error") is None


def test_cron_skips_delivery_only_for_exact_silent_marker(monkeypatch, tmp_path):
    count, delivered, marked = _run_single_due_job(monkeypatch, tmp_path, "  [SILENT]\n")

    assert count == 1
    assert delivered == []
    assert marked
    args, kwargs = marked[0]
    assert args[:3] == ("abc123abc123", True, None)
    assert kwargs.get("delivery_error") is None
