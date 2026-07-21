import logging

from tools.delegation_scope import (
    delegation_authority_audit_view,
    log_delegation_authority_event,
)


def test_audit_view_retains_visible_authority_and_redacts_host_backing():
    raw = {
        "profile": {
            "name": "protected",
            "hash": "profile-hash",
            "snapshot": {"image": "repo/image@sha256:deadbeef"},
        },
        "workdir": "/workspace",
        "reveal": [{"path": "/workspace/data", "mode": "ro"}],
        "visible_objects": [{
            "path": "/workspace/data",
            "mode": "ro",
            "object_type": "directory",
            "backing": {
                "object_id": "obj-1",
                "kind": "host_path",
                "identity": "/home/operator/private/data",
                "revision": "dev:ino:rev",
            },
        }],
        "tools": {"enabled_toolsets": ["terminal"], "disabled_toolsets": []},
        "lineage": {"scope_id": "scope-1", "attempt_id": "attempt-1"},
        "state": {"revoked": False, "cleaned": False},
    }

    audit = delegation_authority_audit_view(raw)

    assert audit["profile"]["name"] == "protected"
    assert audit["workdir"] == "/workspace"
    assert audit["visible_objects"][0]["path"] == "/workspace/data"
    assert audit["visible_objects"][0]["backing"] == {
        "object_id": "obj-1",
        "kind": "host_path",
        "identity": "[REDACTED]",
        "revision": "dev:ino:rev",
    }
    assert audit["implicit_mounts_suppressed"] is True
    assert "/home/operator/private/data" not in repr(audit)


def test_structured_audit_event_never_logs_backing_identity(caplog):
    raw = {
        "profile": {
            "name": "protected",
            "hash": "profile-hash",
            "snapshot": {"image": "repo/image@sha256:deadbeef"},
        },
        "workdir": "/workspace",
        "reveal": [{"path": "/workspace/data", "mode": "ro"}],
        "visible_objects": [{
            "path": "/workspace/data",
            "mode": "ro",
            "object_type": "directory",
            "backing": {
                "object_id": "obj-1",
                "kind": "host_path",
                "identity": "/home/operator/private/data",
                "revision": "dev:ino:rev",
            },
        }],
        "tools": {"enabled_toolsets": ["terminal"], "disabled_toolsets": []},
        "lineage": {"scope_id": "scope-1", "attempt_id": "attempt-1"},
        "state": {"revoked": False, "cleaned": False},
    }

    with caplog.at_level(logging.INFO, logger="tools.delegation_scope"):
        event = log_delegation_authority_event("attempt_started", raw)

    assert event["event"] == "attempt_started"
    assert event["authority"]["visible_objects"][0]["backing"]["identity"] == "[REDACTED]"
    assert "/home/operator/private/data" not in caplog.text


def test_audit_reports_runtime_location_network_and_qualified_external_capabilities():
    raw = {
        "profile": {
            "name": "browser-protected",
            "hash": "profile-hash",
            "snapshot": {
                "backend": "docker",
                "image": "repo/browser@sha256:deadbeef",
                "network": "none",
                "allowed_toolsets": ["terminal", "mcp-safe"],
                "qualified_mcp_servers": ["safe-server"],
            },
        },
        "workdir": "/workspace",
        "reveal": [],
        "visible_objects": [],
        "tools": {
            "enabled_toolsets": ["terminal", "mcp-safe"],
            "disabled_toolsets": [],
        },
        "lineage": {"scope_id": "scope-1", "attempt_id": "attempt-1"},
        "state": {"revoked": False, "cleaned": False},
    }

    audit = delegation_authority_audit_view(raw)

    assert audit["network_mode"] == "none"
    assert audit["browser"] == {
        "location": "attempt_container",
        "mode": "terminal_or_code_only",
        "host_browser_tools_admitted": False,
    }
    assert audit["mcp"] == {
        "location": "external_operator_qualified",
        "qualified_servers": ["safe-server"],
    }
    assert audit["environment"] == {
        "backend": "docker",
        "owner": "attempt-1",
        "scope_id": "scope-1",
    }
    assert audit["outcome"] == {
        "creation": "authority_reserved",
        "cleanup": "pending",
    }
