from __future__ import annotations

import pytest

from agent.delegation_policy import (
    AccessMode,
    BackingObjectRef,
    DelegationSessionPolicy,
    ExecutionProfile,
    VisibleObjectGrant,
    derive_child_policy,
)
from tools.delegation_scope import (
    BackingObjectRecord,
    BackingObjectRegistry,
    execution_profile_hash,
    parse_execution_profiles,
    resolve_invocation_scope,
    serialize_delegation_authority,
    delegation_authority_audit_view,
)


def _profile(name: str = "isolated", *, backend: str = "docker") -> ExecutionProfile:
    return ExecutionProfile(
        name=name,
        backend=backend,
        image="example@sha256:abc",
        default_workdir="/workspace",
        allowed_toolsets={"terminal", "file"},
    )


def _policy(*, required: bool = True, profiles=("isolated",), visible_objects=()):
    snapshots = {name: _profile(name) for name in profiles}
    return DelegationSessionPolicy(
        profile_required=required,
        allow_profile_none=not required,
        allowed_profiles=frozenset(profiles),
        profile_snapshots=snapshots,
        visible_objects=tuple(visible_objects),
        protected_prefixes=("/protected",),
    )


def _grant(path: str, mode: AccessMode = AccessMode.RW, *, object_id: str = "obj-1"):
    return VisibleObjectGrant(
        visible_path=path,
        mode=mode,
        backing=BackingObjectRef(object_id, "host_path", f"identity:{object_id}", "rev-1"),
        object_type="directory",
    )


def _record(grant: VisibleObjectGrant, **overrides):
    values = {
        "backing": grant.backing,
        "object_type": grant.object_type,
        "exists": True,
        "root_symlink": False,
    }
    values.update(overrides)
    return BackingObjectRecord(**values)


def test_nested_scope_cannot_restore_an_omitted_sibling():
    root = _grant("/workspace/root", object_id="root")
    sibling = _grant("/workspace/sibling", object_id="sibling")
    parent = _policy(visible_objects=(root, sibling))
    child = derive_child_policy(
        parent,
        (_grant("/workspace/root", AccessMode.RO, object_id="root"),),
        allowed_profiles={"isolated"},
    )

    with pytest.raises(ValueError, match="outside parent/session ceiling"):
        resolve_invocation_scope(
            child,
            "isolated",
            "/workspace",
            [{"path": "/workspace/sibling", "mode": "ro"}],
        )


def test_nested_scope_cannot_widen_read_only_grant_to_read_write():
    parent = _policy(
        visible_objects=(_grant("/workspace/root", object_id="root"),)
    )
    child = derive_child_policy(
        parent,
        (_grant("/workspace/root", AccessMode.RO, object_id="root"),),
        allowed_profiles={"isolated"},
    )

    with pytest.raises(ValueError, match="cannot widen"):
        resolve_invocation_scope(
            child,
            "isolated",
            "/workspace",
            [{"path": "/workspace/root", "mode": "rw"}],
        )


@pytest.mark.parametrize("profile", [None, "", "none", "default", "shared"])
def test_protected_policy_rejects_unprofiled_and_fallback_profiles(profile):
    with pytest.raises(ValueError, match="profile is required"):
        resolve_invocation_scope(_policy(), profile, None, None)


def test_ordinary_omission_preserves_legacy_unprofiled_scope():
    assert resolve_invocation_scope(None, None, None, None) is None


@pytest.mark.parametrize(
    ("session_network", "effective_mode"), [(False, "none"), (True, "full")]
)
def test_inherited_network_is_frozen_to_admitted_session_setting(
    session_network, effective_mode
):
    profile = ExecutionProfile(
        name="isolated",
        backend="docker",
        image="example@sha256:abc",
        default_workdir="/workspace",
        allowed_toolsets={"terminal"},
        network="inherit",
    )
    policy = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles=frozenset({"isolated"}),
        profile_snapshots={"isolated": profile},
        visible_objects=(),
        protected_prefixes=(),
    )

    scope = resolve_invocation_scope(
        policy,
        "isolated",
        None,
        None,
        inherited_network=session_network,
    )
    assert scope is not None
    assert scope.profile.network == effective_mode
    authority = serialize_delegation_authority(
        scope,
        enabled_toolsets=(),
        disabled_toolsets=(),
        scope_id="scope-network",
        attempt_id="attempt-network",
    )
    assert delegation_authority_audit_view(authority)["network_mode"] == effective_mode


@pytest.mark.parametrize(
    ("profile", "workdir", "reveal"),
    [
        ("isolated", None, None),
        (None, "/workspace", None),
        (None, None, [{"path": "/work/input", "mode": "ro"}]),
    ],
)
def test_ordinary_session_rejects_unadmitted_scope_fields(profile, workdir, reveal):
    with pytest.raises(ValueError, match="admitted protected profile"):
        resolve_invocation_scope(None, profile, workdir, reveal)


@pytest.mark.parametrize("case", ["unknown", "disallowed", "stale", "non-docker"])
def test_profile_resolution_rejects_unknown_disallowed_stale_or_non_docker(case):
    isolated = _profile("isolated")
    snapshots = {"isolated": isolated}
    allowed = {"isolated"}
    selected = "missing"
    if case == "disallowed":
        snapshots["other"] = _profile("other")
        selected = "other"
    elif case == "stale":
        snapshots["isolated"] = _profile("renamed")
        selected = "isolated"
    elif case == "non-docker":
        snapshots["isolated"] = _profile("isolated", backend="local")
        selected = "isolated"
    policy = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles=allowed,
        profile_snapshots=snapshots,
        visible_objects=(),
        protected_prefixes=("/protected",),
    )

    with pytest.raises(ValueError, match="unknown|not allowed|stale|Docker"):
        resolve_invocation_scope(policy, selected, None, None)


def test_profile_parser_rejects_unknown_authority_expanding_keys():
    config = {
        "filesystem_isolation": {
            "profiles": {
                "isolated": {
                    "backend": "docker",
                    "image": "example@sha256:abc",
                    "default_workdir": "/workspace",
                    "allowed_toolsets": ["terminal"],
                    "volumes": ["/host:/container"],
                }
            }
        }
    }
    with pytest.raises(ValueError, match="unknown profile keys.*volumes"):
        parse_execution_profiles(config)


def test_profile_parser_builds_immutable_snapshot_with_stable_canonical_hash():
    config = {
        "filesystem_isolation": {
            "profiles": {
                "isolated": {
                    "backend": "docker",
                    "image": "example@sha256:abc",
                    "default_workdir": "/workspace",
                    "allowed_toolsets": ["terminal", "file"],
                    "qualified_mcp_servers": ["safe-server"],
                    "network": "inherit",
                    "cpu": 2,
                    "memory_mb": 4096,
                    "shm_mb": 1024,
                    "pids_limit": 512,
                }
            }
        }
    }

    parsed = parse_execution_profiles(config)

    assert tuple(parsed) == ("isolated",)
    assert parsed["isolated"].allowed_toolsets == frozenset({"terminal", "file"})
    assert len(execution_profile_hash(parsed["isolated"])) == 64
    equivalent = ExecutionProfile(
        name="isolated",
        backend="docker",
        image="example@sha256:abc",
        default_workdir="/workspace",
        allowed_toolsets={"file", "terminal"},
        qualified_mcp_servers=frozenset({"safe-server"}),
        network="inherit",
        cpu=2,
        memory_mb=4096,
        shm_mb=1024,
        pids_limit=512,
    )
    assert execution_profile_hash(parsed["isolated"]) == execution_profile_hash(equivalent)
    with pytest.raises(TypeError):
        parsed["other"] = _profile("other")  # type: ignore[index]


@pytest.mark.parametrize(
    "reveal",
    [
        "not-a-list",
        ["not-an-object"],
        [{"path": "/work/input"}],
        [{"path": "/work/input", "mode": "read"}],
        [{"path": "/work/input", "mode": "ro", "source": "/host"}],
    ],
)
def test_reveal_declaration_and_mode_are_strict(reveal):
    policy = _policy(visible_objects=(_grant("/work/input"),))
    with pytest.raises(ValueError, match="reveal"):
        resolve_invocation_scope(policy, "isolated", None, reveal)


@pytest.mark.parametrize("path", ["relative/input", "/work/../secret", "/work//input"])
def test_reveal_path_must_be_canonical_absolute_without_parent_escape(path):
    policy = _policy(visible_objects=(_grant("/work/input"),))
    with pytest.raises(ValueError, match="canonical absolute"):
        resolve_invocation_scope(
            policy, "isolated", None, [{"path": path, "mode": "ro"}]
        )


def test_reveal_must_match_a_registered_object_in_parent_ceiling():
    policy = _policy(visible_objects=(_grant("/work/input"),))
    with pytest.raises(ValueError, match="outside parent/session ceiling"):
        resolve_invocation_scope(
            policy,
            "isolated",
            None,
            [{"path": "/work/hidden", "mode": "ro"}],
        )


def test_reveal_cannot_request_rw_over_parent_ro_ceiling():
    policy = _policy(visible_objects=(_grant("/work/input", AccessMode.RO),))
    with pytest.raises(ValueError, match="cannot widen"):
        resolve_invocation_scope(
            policy,
            "isolated",
            None,
            [{"path": "/work/input", "mode": "rw"}],
        )


def test_backing_registry_rejects_nonexistent_registered_object():
    grant = _grant("/work/input")
    policy = _policy(visible_objects=(grant,))
    registry = BackingObjectRegistry(
        {grant.backing.object_id: _record(grant, exists=False)}
    )
    with pytest.raises(ValueError, match="no longer exists"):
        resolve_invocation_scope(
            policy,
            "isolated",
            None,
            [{"path": "/work/input", "mode": "ro"}],
            backing_registry=registry,
        )


@pytest.mark.parametrize("case", ["root-symlink", "changed-revision", "changed-identity"])
def test_backing_registry_rejects_symlink_or_changed_identity(case):
    grant = _grant("/work/input")
    overrides = {}
    if case == "root-symlink":
        overrides["root_symlink"] = True
    else:
        overrides["backing"] = BackingObjectRef(
            grant.backing.object_id,
            grant.backing.kind,
            "changed-identity" if case == "changed-identity" else grant.backing.identity,
            "rev-2" if case == "changed-revision" else grant.backing.revision,
        )
    registry = BackingObjectRegistry(
        {grant.backing.object_id: _record(grant, **overrides)}
    )
    with pytest.raises(ValueError, match="symlink|identity|revision"):
        resolve_invocation_scope(
            _policy(visible_objects=(grant,)),
            "isolated",
            None,
            [{"path": "/work/input", "mode": "ro"}],
            backing_registry=registry,
        )


@pytest.mark.parametrize(
    "path",
    [
        "/home/prod/project",
        "/home/prod/.hermes/hermes-agent",
        "/work/.env",
        "/protected/child",
        "/var/run/docker.sock",
    ],
)
def test_reveal_rejects_protected_host_and_credential_paths(path):
    grant = _grant(path)
    with pytest.raises(ValueError, match="protected|forbidden|credential|Docker socket"):
        resolve_invocation_scope(
            _policy(visible_objects=(grant,)),
            "isolated",
            None,
            [{"path": path, "mode": "ro"}],
        )


@pytest.mark.parametrize("object_type", ["device", "socket", "fifo"])
def test_backing_registry_rejects_special_object_types(object_type):
    grant = _grant("/work/input")
    registry = BackingObjectRegistry(
        {grant.backing.object_id: _record(grant, object_type=object_type)}
    )
    with pytest.raises(ValueError, match="regular file or directory"):
        resolve_invocation_scope(
            _policy(visible_objects=(grant,)),
            "isolated",
            None,
            [{"path": "/work/input", "mode": "ro"}],
            backing_registry=registry,
        )


@pytest.mark.parametrize("case", ["duplicate", "overlap-conflict"])
def test_reveal_rejects_duplicate_or_conflicting_overlaps(case):
    parent = _grant("/work/tree", object_id="parent")
    child = _grant("/work/tree/child", object_id="child")
    policy = _policy(visible_objects=(parent, child))
    reveal = [
        {"path": "/work/tree", "mode": "ro"},
        {
            "path": "/work/tree" if case == "duplicate" else "/work/tree/child",
            "mode": "ro" if case == "duplicate" else "rw",
        },
    ]
    with pytest.raises(ValueError, match="duplicate|overlap"):
        resolve_invocation_scope(policy, "isolated", None, reveal)


def test_valid_leaf_resolution_excludes_siblings_and_attenuates_mode():
    leaf = _grant("/work/input/leaf", object_id="leaf")
    sibling = _grant("/work/input/sibling", object_id="sibling")
    policy = _policy(visible_objects=(leaf, sibling))

    scope = resolve_invocation_scope(
        policy,
        "isolated",
        None,
        [{"path": "/work/input/leaf", "mode": "ro"}],
    )

    assert scope is not None
    assert scope.profile_name == "isolated"
    assert scope.profile is policy.profile_snapshots["isolated"]
    assert scope.profile_hash == execution_profile_hash(scope.profile)
    assert scope.workdir.as_posix() == "/workspace"
    assert [(str(item.visible_path), item.mode.value) for item in scope.visible_objects] == [
        ("/work/input/leaf", "ro")
    ]


@pytest.mark.parametrize(
    "case",
    ["relative", "parent-escape", "unrevealed", "revealed-ro", "revealed-file"],
)
def test_workdir_rejects_noncanonical_unrevealed_or_nonwritable_storage(case):
    grant = _grant("/work/input")
    reveal = []
    workdir = {
        "relative": "relative/path",
        "parent-escape": "/workspace/../etc",
        "unrevealed": "/etc",
        "revealed-ro": "/work/input/subdir",
        "revealed-file": "/work/input",
    }[case]
    if case == "revealed-ro":
        reveal = [{"path": "/work/input", "mode": "ro"}]
    elif case == "revealed-file":
        grant = VisibleObjectGrant(
            visible_path=grant.visible_path,
            mode=grant.mode,
            backing=grant.backing,
            object_type="file",
        )
        reveal = [{"path": "/work/input", "mode": "rw"}]
    policy = _policy(visible_objects=(grant,))

    with pytest.raises(ValueError, match="workdir"):
        resolve_invocation_scope(policy, "isolated", workdir, reveal)


@pytest.mark.parametrize("workdir", ["/workspace/subdir", "/tmp/job", "/work/input/subdir"])
def test_workdir_accepts_container_local_or_rw_revealed_directory(workdir):
    grant = _grant("/work/input")
    scope = resolve_invocation_scope(
        _policy(visible_objects=(grant,)),
        "isolated",
        workdir,
        [{"path": "/work/input", "mode": "rw"}],
    )
    assert scope is not None
    assert str(scope.workdir) == workdir
