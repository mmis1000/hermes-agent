"""Tests for the bundled observability/token_usage_report plugin."""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "observability" / "token_usage_report"


class TestManifest:
    def test_plugin_directory_exists(self):
        assert PLUGIN_DIR.is_dir()
        assert (PLUGIN_DIR / "plugin.yaml").exists()
        assert (PLUGIN_DIR / "__init__.py").exists()
        assert (PLUGIN_DIR / "README.md").exists()

    def test_manifest_uses_current_request_hook(self):
        data = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
        assert data["name"] == "token_usage_report"
        assert data["version"]
        assert data["hooks"] == ["post_api_request"]


class TestDiscovery:
    def test_plugin_is_discovered_but_opt_in_by_default(self, tmp_path, monkeypatch):
        from hermes_cli import plugins as plugins_mod

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        manager = plugins_mod.PluginManager()
        manager.discover_and_load()

        loaded = manager._plugins.get("observability/token_usage_report")
        assert loaded is not None
        assert loaded.enabled is False
        assert "not enabled" in (loaded.error or "").lower()


class TestReportWriter:
    @staticmethod
    def _fresh_plugin():
        mod_name = "plugins.observability.token_usage_report"
        sys.modules.pop(mod_name, None)
        return importlib.import_module(mod_name)

    def test_post_api_request_writes_strict_jsonl_and_markdown(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_TOKEN_USAGE_REPORT_DIR", str(tmp_path))
        monkeypatch.setenv("HERMES_TOKEN_USAGE_REPORT_MAX_EVENTS", "10")
        monkeypatch.setenv("HERMES_TOKEN_USAGE_REPORT_RECENT_ROWS", "5")
        plugin = self._fresh_plugin()

        plugin.on_post_api_request(
            session_id="session-1",
            turn_id="turn-1",
            api_request_id="turn-1:codex-token-usage:1",
            source="codex_app_server",
            model="gpt-5.5",
            response_model="gpt-5.5",
            provider="openai-codex",
            api_mode="codex_app_server",
            api_call_count=1,
            session_api_call_count=7,
            usage={
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "input_tokens": 100,
                "cache_read_tokens": 20,
                "cache_write_tokens": 0,
                "output_tokens": 30,
                "reasoning_tokens": 516,
            },
            raw_usage={
                "inputTokens": 100,
                "cachedInputTokens": 20,
                "outputTokens": 30,
                "reasoningOutputTokens": 516,
                "totalTokens": 666,
            },
            codex_thread_id="thread-1",
        )

        events_path = tmp_path / "events.jsonl"
        report_path = tmp_path / "latest.md"
        rows = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
        ]
        assert len(rows) == 1
        assert rows[0]["reasoning_tokens"] == 516
        assert rows[0]["reported_total_tokens"] == 666
        assert rows[0]["raw_usage"]["reasoningOutputTokens"] == 516
        json.dumps(rows[0], allow_nan=False)

        report = report_path.read_text(encoding="utf-8")
        assert "# Hermes token usage report" in report
        assert "gpt-5.5" in report
        assert "516" in report
        assert "100.0%" in report

    def test_malformed_numbers_and_nested_raw_values_are_sanitized(self):
        plugin = self._fresh_plugin()
        event = plugin._event_from_kwargs(
            {
                "model": "model|evil\r\nnext",
                "usage": {
                    "input_tokens": float("inf"),
                    "cache_read_tokens": float("-inf"),
                    "output_tokens": -3,
                    "reasoning_tokens": True,
                    "estimated_cost_usd": float("nan"),
                },
                "raw_usage": {
                    "nested": [float("nan"), {"inf": float("inf")}, 7],
                },
            }
        )

        assert event["input_tokens"] == 0
        assert event["cache_read_tokens"] == 0
        assert event["output_tokens"] == 0
        assert event["reasoning_tokens"] == 0
        assert "estimated_cost_usd" not in event
        assert event["raw_usage"]["nested"] == [None, {"inf": None}, 7]
        json.dumps(event, allow_nan=False)

        report = plugin._render_report([event], (516,))
        assert "model\\|evil<br>next" in report
        assert "model|evil\r\nnext" not in report

    def test_recent_event_loader_reads_bounded_valid_tail(self, tmp_path):
        plugin = self._fresh_plugin()
        events_path = tmp_path / "events.jsonl"
        with events_path.open("w", encoding="utf-8") as fh:
            for index in range(300):
                fh.write(json.dumps({"index": index}) + "\n")
            fh.write("{malformed\n")

        events = plugin._load_recent_events(events_path, 10)
        assert [event["index"] for event in events] == list(range(290, 300))

    def test_recent_event_loader_caps_giant_unterminated_tail_bytes(
        self, tmp_path, monkeypatch
    ):
        plugin = self._fresh_plugin()
        monkeypatch.setattr(plugin, "_MAX_TAIL_SCAN_BYTES", 1024)
        events_path = tmp_path / "events.jsonl"
        events_path.write_bytes(b'{"index": 1}\n' + (b"x" * 4096))

        assert plugin._load_recent_events(events_path, 10) == []

    def test_concurrent_processes_write_complete_jsonl_rows(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_TOKEN_USAGE_REPORT_DIR", str(tmp_path))
        script = """
import sys
from plugins.observability.token_usage_report import on_post_api_request
worker = int(sys.argv[1])
for index in range(8):
    on_post_api_request(
        session_id=f"worker-{worker}",
        turn_id=f"turn-{worker}-{index}",
        api_request_id=f"request-{worker}-{index}",
        model="test/model",
        usage={"input_tokens": index, "output_tokens": 1, "reasoning_tokens": index},
    )
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(worker)],
                cwd=REPO_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for worker in range(4)
        ]
        failures = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            if process.returncode != 0:
                failures.append((process.returncode, stdout, stderr))

        assert failures == []
        rows = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert len(rows) == 32
        assert len({row["api_request_id"] for row in rows}) == 32
        assert all(isinstance(row, dict) for row in rows)
        assert (tmp_path / "latest.md").read_text(encoding="utf-8").startswith(
            "# Hermes token usage report"
        )
