import asyncio
import base64
import json
from pathlib import PurePosixPath

from agent.delegation_policy import ExecutionProfile
from tools import file_tools, read_extract
from tools import image_source
from tools.delegation_scope import (
    ResolvedInvocationScope,
    attempt_scope_registry,
    execution_profile_hash,
)


def _scope():
    profile = ExecutionProfile(
        "protected", "docker", "repo/protected@sha256:deadbeef", "/workspace",
        frozenset({"file"}),
    )
    return ResolvedInvocationScope(
        profile.name, execution_profile_hash(profile), profile,
        PurePosixPath("/workspace"), (), (),
    )


def test_protected_document_read_uses_scoped_bytes_not_host_path(monkeypatch):
    authority = attempt_scope_registry.reserve(
        _scope(), "logical-doc", attempt_id="attempt-doc"
    )
    payload = json.dumps({
        "cells": [{"cell_type": "markdown", "source": ["inside scope"]}],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }).encode()

    class ScopedOps:
        def read_bytes(self, path):
            assert path == "/workspace/report.ipynb"
            return payload

        @staticmethod
        def _add_line_numbers(text, offset):
            return f"{offset}|{text}"

    monkeypatch.setattr(file_tools, "_get_file_ops", lambda _task_id: ScopedOps())
    monkeypatch.setattr(
        file_tools,
        "_resolve_path_for_task",
        lambda _path, _task_id: PurePosixPath("/workspace/report.ipynb"),
    )
    monkeypatch.setattr(
        read_extract,
        "extract_document_text",
        lambda _path: (_ for _ in ()).throw(AssertionError("host path read")),
    )

    try:
        result = json.loads(file_tools.read_file_tool(
            "report.ipynb", task_id=authority.attempt_id
        ))
    finally:
        attempt_scope_registry.cleanup(authority.attempt_id)

    assert result["extracted_document"] is True
    assert "inside scope" in result["content"]
    assert result["file_size"] == len(payload)


def test_protected_image_path_uses_attempt_environment_even_when_host_backend_is_local(
    monkeypatch, tmp_path
):
    authority = attempt_scope_registry.reserve(
        _scope(), "logical-image", attempt_id="attempt-image"
    )
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    hidden_host_path = tmp_path / "host-only.png"
    hidden_host_path.write_bytes(png)

    class ScopedEnv:
        def execute(self, command):
            assert str(hidden_host_path) in command
            return {
                "returncode": 0,
                "output": base64.b64encode(png).decode(),
            }

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(
        "tools.terminal_tool.get_active_env",
        lambda task_id: ScopedEnv() if task_id == authority.attempt_id else None,
    )

    try:
        resolved = asyncio.run(image_source.resolve_image_source(
            str(hidden_host_path),
            image_source.ResolveContext(task_id=authority.attempt_id),
        ))
    finally:
        attempt_scope_registry.cleanup(authority.attempt_id)

    assert resolved.origin == "container"
