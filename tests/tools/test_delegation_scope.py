from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile

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
    RevealRequest,
    ResolvedInvocationScope,
    admit_trusted_run_execution as _admit_trusted_run_execution,
    delegation_authority_audit_view,
    deserialize_delegation_authority,
    execution_profile_hash,
    format_effective_scope_context,
    parse_execution_profiles,
    resolve_invocation_scope,
    serialize_delegation_authority,
)


def admit_trusted_run_execution(
    policy,
    execution,
    *,
    inherited_network: bool = False,
):
    return _admit_trusted_run_execution(
        policy,
        execution,
        inherited_network=inherited_network,
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
        visible_objects=(
            None if visible_objects is None else tuple(visible_objects)
        ),
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


def test_effective_scope_context_contains_only_agent_visible_filesystem_contract():
    profile = _profile()
    writable = _grant("/work/project", AccessMode.RW, object_id="project-object")
    readonly = VisibleObjectGrant(
        visible_path="/references/资料`line\nbreak.pdf",
        mode=AccessMode.RO,
        backing=BackingObjectRef(
            "spec-object", "host_path", "/host/private/spec.pdf", "secret-revision"
        ),
        object_type="file",
    )
    scope = ResolvedInvocationScope(
        profile_name=profile.name,
        profile_hash=execution_profile_hash(profile),
        profile=profile,
        workdir=PurePosixPath("/work/project"),
        reveal=(
            RevealRequest(PurePosixPath("/work/project"), "rw"),
            RevealRequest(PurePosixPath("/references/资料`line\nbreak.pdf"), "ro"),
        ),
        visible_objects=(writable, readonly),
    )

    context = format_effective_scope_context(scope)

    assert context == (
        "## Execution filesystem\n"
        "- Working directory: \"/work/project\"\n"
        "- Available paths:\n"
        "  - \"/work/project\" — directory, read-write\n"
        "  - \"/references/资料`line\\nbreak.pdf\" — file, read-only\n"
        "- Other host paths are not available in this attempt."
    )
    for hidden in (
        profile.name,
        execution_profile_hash(profile),
        profile.image,
        "project-object",
        "spec-object",
        "/host/private/spec.pdf",
        "secret-revision",
    ):
        assert hidden not in context


def test_trusted_run_execution_admits_real_directory_as_root_scope(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    policy = _policy(profiles=("isolated",))

    admitted = admit_trusted_run_execution(
        policy,
        {
            "profile": "isolated",
            "workdir": str(repository),
            "reveal": [{"path": str(repository), "mode": "rw"}],
        },
    )

    assert admitted.invocation_scope.profile_name == "isolated"
    assert admitted.invocation_scope.workdir == PurePosixPath(str(repository))
    assert admitted.invocation_scope.visible_objects == admitted.policy.visible_objects
    grant = admitted.policy.visible_objects[0]
    assert grant.visible_path == PurePosixPath(str(repository))
    assert grant.mode is AccessMode.RW
    assert grant.object_type == "directory"
    record = admitted.backing_registry.get(grant.backing.object_id)
    assert record is not None
    assert record.backing == grant.backing
    assert record.trusted_host_path is True
    assert admitted.invocation_scope.skill_names == frozenset()
    assert admitted.invocation_scope.all_skills is False


def test_trusted_run_execution_admits_exact_regular_file(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("project context", encoding="utf-8")

    admitted = admit_trusted_run_execution(
        _policy(profiles=("isolated",)),
        {
            "profile": "isolated",
            "workdir": "/workspace",
            "reveal": [{"path": str(readme), "mode": "ro"}],
        },
    )

    grant = admitted.invocation_scope.visible_objects[0]
    assert grant.visible_path == PurePosixPath(str(readme))
    assert grant.mode is AccessMode.RO
    assert grant.object_type == "file"
    record = admitted.backing_registry.get(grant.backing.object_id)
    assert record is not None
    assert record.object_type == "file"
    assert record.backing == grant.backing
    assert admitted.invocation_scope.workdir == PurePosixPath("/workspace")


def test_trusted_run_execution_admits_specific_skill_names(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(
        "tools.skills_tool._find_all_skills",
        lambda: [{"name": "first"}, {"name": "second"}],
    )
    monkeypatch.setattr(
        "tools.skills_tool.skill_view",
        lambda name, **_kwargs: json.dumps(
            {"success": name in {"first", "second"}}
        ),
    )

    admitted = admit_trusted_run_execution(
        _policy(profiles=("isolated",)),
        {
            "profile": "isolated",
            "workdir": str(repository),
            "reveal": [{"path": str(repository), "mode": "rw"}],
            "skills": ["second", "first", "second"],
        },
    )

    assert admitted.invocation_scope.skill_names == frozenset({"first", "second"})
    assert admitted.invocation_scope.all_skills is False


def test_trusted_run_execution_admits_explicit_all_skills(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()

    admitted = admit_trusted_run_execution(
        _policy(profiles=("isolated",)),
        {
            "profile": "isolated",
            "workdir": str(repository),
            "reveal": [{"path": str(repository), "mode": "rw"}],
            "skills": "all",
        },
    )

    assert admitted.invocation_scope.skill_names == frozenset()
    assert admitted.invocation_scope.all_skills is True


@pytest.mark.parametrize("skills", [None, "selected", {}, [""], [1]])
def test_trusted_run_execution_rejects_invalid_skill_grant(
    tmp_path,
    skills,
):
    repository = tmp_path / "repository"
    repository.mkdir()

    with pytest.raises((TypeError, ValueError), match="skills"):
        admit_trusted_run_execution(
            _policy(profiles=("isolated",)),
            {
                "profile": "isolated",
                "workdir": str(repository),
                "reveal": [{"path": str(repository), "mode": "rw"}],
                "skills": skills,
            },
        )


def test_trusted_run_execution_rejects_unknown_skill(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(
        "tools.skills_tool._find_all_skills",
        lambda: [{"name": "known"}],
    )

    with pytest.raises(ValueError, match="unknown skill.*missing"):
        admit_trusted_run_execution(
            _policy(profiles=("isolated",)),
            {
                "profile": "isolated",
                "workdir": str(repository),
                "reveal": [{"path": str(repository), "mode": "rw"}],
                "skills": ["missing"],
            },
        )


def test_trusted_run_execution_rejects_unviewable_skill_name(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(
        "tools.skills_tool._find_all_skills",
        lambda: [{"name": "duplicate"}],
    )
    monkeypatch.setattr(
        "tools.skills_tool.skill_view",
        lambda *_args, **_kwargs: json.dumps(
            {"success": False, "error": "ambiguous skill name"}
        ),
    )

    with pytest.raises(ValueError, match="not uniquely viewable"):
        admit_trusted_run_execution(
            _policy(profiles=("isolated",)),
            {
                "profile": "isolated",
                "workdir": str(repository),
                "reveal": [{"path": str(repository), "mode": "rw"}],
                "skills": ["duplicate"],
            },
        )


@pytest.mark.parametrize(
    ("session_network", "effective_mode"),
    [(False, "none"), (True, "full")],
)
def test_trusted_run_execution_freezes_inherited_network(
    tmp_path,
    session_network,
    effective_mode,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    admitted = admit_trusted_run_execution(
        _policy(profiles=("isolated",)),
        {
            "profile": "isolated",
            "workdir": str(repository),
            "reveal": [{"path": str(repository), "mode": "rw"}],
        },
        inherited_network=session_network,
    )

    assert admitted.invocation_scope.profile.network == effective_mode
    assert admitted.invocation_scope.profile_template_hash is not None


def test_trusted_run_execution_can_admit_exact_directory_below_home():
    with tempfile.TemporaryDirectory(dir=Path.home()) as repository:
        admitted = admit_trusted_run_execution(
            _policy(profiles=("isolated",)),
            {
                "profile": "isolated",
                "workdir": repository,
                "reveal": [{"path": repository, "mode": "rw"}],
            },
        )

    assert admitted.invocation_scope.workdir == PurePosixPath(repository)


def test_trusted_run_execution_admits_operator_selected_home_root():
    home = str(Path.home().resolve())

    admitted = admit_trusted_run_execution(
        _policy(profiles=("isolated",)),
        {
            "profile": "isolated",
            "workdir": home,
            "reveal": [{"path": home, "mode": "rw"}],
        },
    )

    assert admitted.invocation_scope.workdir == PurePosixPath(home)


@pytest.mark.parametrize(
    "selected_root",
    ["/usr", "/etc", "/proc", "/sys", "/dev", "/var/lib"],
)
def test_trusted_run_execution_admits_operator_selected_existing_root(
    selected_root,
):
    admitted = admit_trusted_run_execution(
        _policy(profiles=("isolated",)),
        {
            "profile": "isolated",
            "workdir": selected_root,
            "reveal": [{"path": selected_root, "mode": "rw"}],
        },
    )

    assert admitted.invocation_scope.workdir == PurePosixPath(selected_root)


@pytest.mark.parametrize(
    "relative_path",
    [
        ".config",
        ".kube",
        ".docker",
        ".pki",
        ".local/share/keyrings",
    ],
)
def test_trusted_run_execution_admits_operator_selected_named_directory(
    tmp_path,
    relative_path,
):
    selected = tmp_path / relative_path
    selected.mkdir(parents=True)

    admitted = admit_trusted_run_execution(
        _policy(profiles=("isolated",)),
        {
            "profile": "isolated",
            "workdir": str(selected),
            "reveal": [{"path": str(selected), "mode": "rw"}],
        },
    )

    assert admitted.invocation_scope.workdir == PurePosixPath(str(selected))


def test_trusted_run_backing_tracks_canonical_path_after_directory_replacement(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    admitted = admit_trusted_run_execution(
        _policy(profiles=("isolated",)),
        {
            "profile": "isolated",
            "workdir": str(repository),
            "reveal": [{"path": str(repository), "mode": "rw"}],
        },
    )
    grant = admitted.policy.visible_objects[0]
    repository.rename(tmp_path / "original")
    repository.mkdir()

    assert admitted.backing_registry.get(grant.backing.object_id) is not None


def test_trusted_root_can_attenuate_to_existing_child_directory(tmp_path):
    repository = tmp_path / "repository"
    lane = repository / "lanes" / "source-blind"
    sibling = repository / "lanes" / "reference"
    lane.mkdir(parents=True)
    sibling.mkdir()
    admitted = admit_trusted_run_execution(
        _policy(profiles=("isolated",)),
        {
            "profile": "isolated",
            "workdir": str(repository),
            "reveal": [{"path": str(repository), "mode": "rw"}],
        },
    )

    child_scope = resolve_invocation_scope(
        admitted.policy,
        "isolated",
        None,
        [{"path": str(lane), "mode": "ro"}],
        backing_registry=admitted.backing_registry,
    )

    assert child_scope is not None
    assert child_scope.workdir == PurePosixPath("/workspace")
    assert len(child_scope.visible_objects) == 1
    assert child_scope.visible_objects[0].visible_path == PurePosixPath(str(lane))
    assert child_scope.visible_objects[0].mode is AccessMode.RO
    child_policy = derive_child_policy(admitted.policy, child_scope.visible_objects)
    assert child_policy.visible_objects == child_scope.visible_objects
    with pytest.raises(ValueError, match="outside parent/session ceiling"):
        resolve_invocation_scope(
            child_policy,
            "isolated",
            None,
            [{"path": str(sibling), "mode": "ro"}],
            backing_registry=admitted.backing_registry,
        )
    with pytest.raises(ValueError, match="cannot widen"):
        resolve_invocation_scope(
            child_policy,
            "isolated",
            str(lane),
            [{"path": str(lane), "mode": "rw"}],
            backing_registry=admitted.backing_registry,
        )


def test_trusted_root_descendant_rejects_symlink_escape(tmp_path):
    repository = tmp_path / "repository"
    outside = tmp_path / "outside"
    repository.mkdir()
    outside.mkdir()
    escape = repository / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    admitted = admit_trusted_run_execution(
        _policy(profiles=("isolated",)),
        {
            "profile": "isolated",
            "workdir": str(repository),
            "reveal": [{"path": str(repository), "mode": "rw"}],
        },
    )

    with pytest.raises(ValueError, match="real directory below"):
        resolve_invocation_scope(
            admitted.policy,
            "isolated",
            None,
            [{"path": str(escape), "mode": "ro"}],
            backing_registry=admitted.backing_registry,
        )


def test_trusted_root_allows_multiple_disjoint_child_directories(tmp_path):
    repository = tmp_path / "repository"
    first = repository / "lanes" / "first"
    second = repository / "lanes" / "second"
    first.mkdir(parents=True)
    second.mkdir()
    admitted = admit_trusted_run_execution(
        _policy(profiles=("isolated",)),
        {
            "profile": "isolated",
            "workdir": str(repository),
            "reveal": [{"path": str(repository), "mode": "rw"}],
        },
    )

    child_scope = resolve_invocation_scope(
        admitted.policy,
        "isolated",
        str(second),
        [
            {"path": str(first), "mode": "ro"},
            {"path": str(second), "mode": "rw"},
        ],
        backing_registry=admitted.backing_registry,
    )

    assert child_scope is not None
    assert [grant.visible_path for grant in child_scope.visible_objects] == [
        PurePosixPath(str(first)),
        PurePosixPath(str(second)),
    ]


def test_trusted_root_rejects_overlapping_child_directories_even_same_mode(tmp_path):
    repository = tmp_path / "repository"
    lanes = repository / "lanes"
    first = lanes / "first"
    first.mkdir(parents=True)
    admitted = admit_trusted_run_execution(
        _policy(profiles=("isolated",)),
        {
            "profile": "isolated",
            "workdir": str(repository),
            "reveal": [{"path": str(repository), "mode": "rw"}],
        },
    )

    with pytest.raises(ValueError, match="overlapping reveal paths"):
        resolve_invocation_scope(
            admitted.policy,
            "isolated",
            None,
            [
                {"path": str(lanes), "mode": "ro"},
                {"path": str(first), "mode": "ro"},
            ],
            backing_registry=admitted.backing_registry,
        )


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
                    "allowed_tools": ["delegate_task", "skills_list", "skill_view"],
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
    assert parsed["isolated"].allowed_tools == frozenset(
        {"delegate_task", "skills_list", "skill_view"}
    )
    assert len(execution_profile_hash(parsed["isolated"])) == 64
    equivalent = ExecutionProfile(
        name="isolated",
        backend="docker",
        image="example@sha256:abc",
        default_workdir="/workspace",
        allowed_toolsets={"file", "terminal"},
        allowed_tools={"skill_view", "delegate_task", "skills_list"},
        qualified_mcp_servers=frozenset({"safe-server"}),
        network="inherit",
        cpu=2,
        memory_mb=4096,
        shm_mb=1024,
        pids_limit=512,
    )
    assert execution_profile_hash(parsed["isolated"]) == execution_profile_hash(equivalent)
    scope = ResolvedInvocationScope(
        "isolated",
        execution_profile_hash(equivalent),
        equivalent,
        PurePosixPath("/workspace"),
        (),
        (),
    )
    authority = serialize_delegation_authority(
        scope,
        enabled_toolsets=(),
        disabled_toolsets=(),
        scope_id="scope-tools",
        attempt_id="attempt-tools",
    )
    assert authority["profile"]["snapshot"]["allowed_tools"] == [
        "delegate_task",
        "skill_view",
        "skills_list",
    ]
    restored = deserialize_delegation_authority(authority, backing_registry=None)
    assert restored.profile.allowed_tools == equivalent.allowed_tools
    with pytest.raises(TypeError):
        parsed["other"] = _profile("other")  # type: ignore[index]


def test_authority_round_trip_accepts_toolset_admitted_by_exact_profile_tools():
    profile = ExecutionProfile(
        name="isolated",
        backend="docker",
        image="example@sha256:abc",
        default_workdir="/workspace",
        allowed_toolsets={"terminal", "file"},
        allowed_tools={"skills_list", "skill_view"},
    )
    scope = ResolvedInvocationScope(
        profile.name,
        execution_profile_hash(profile),
        profile,
        PurePosixPath("/workspace"),
        (),
        (),
    )
    authority = serialize_delegation_authority(
        scope,
        enabled_toolsets=("file", "skills"),
        disabled_toolsets=(),
        scope_id="scope-exact-tools",
        attempt_id="attempt-exact-tools",
    )

    restored = deserialize_delegation_authority(authority, backing_registry=None)

    assert restored.profile == profile


def test_runtime_identity_is_optional_hashed_and_operator_supplied():
    base = {
        "backend": "docker",
        "image": "example@sha256:abc",
        "default_workdir": "/workspace",
        "allowed_toolsets": ["terminal"],
    }
    without = parse_execution_profiles(
        {"filesystem_isolation": {"profiles": {"isolated": base}}}
    )["isolated"]
    with_identity = parse_execution_profiles(
        {
            "filesystem_isolation": {
                "profiles": {
                    "isolated": {
                        **base,
                        "runtime_identity": {"uid": 0, "gid": 10001},
                    }
                }
            }
        }
    )["isolated"]

    assert without.runtime_identity is None
    assert with_identity.runtime_identity == (0, 10001)
    assert execution_profile_hash(without) != execution_profile_hash(with_identity)

    without_scope = ResolvedInvocationScope(
        without.name,
        execution_profile_hash(without),
        without,
        PurePosixPath("/workspace"),
        (),
        (),
    )
    with_scope = ResolvedInvocationScope(
        with_identity.name,
        execution_profile_hash(with_identity),
        with_identity,
        PurePosixPath("/workspace"),
        (),
        (),
    )
    without_authority = serialize_delegation_authority(
        without_scope,
        enabled_toolsets=(),
        disabled_toolsets=(),
        scope_id="scope-without",
        attempt_id="attempt-without",
    )
    with_authority = serialize_delegation_authority(
        with_scope,
        enabled_toolsets=(),
        disabled_toolsets=(),
        scope_id="scope-with",
        attempt_id="attempt-with",
    )
    assert "runtime_identity" not in without_authority["profile"]["snapshot"]
    assert with_authority["profile"]["snapshot"]["runtime_identity"] == {
        "uid": 0,
        "gid": 10001,
    }


@pytest.mark.parametrize(
    "identity",
    [
        {"uid": True, "gid": 10001},
        {"uid": -1, "gid": 10001},
        {"uid": 10001},
        {"uid": 10001, "gid": 10001, "extra": 1},
    ],
)
def test_runtime_identity_rejects_malformed_values(identity):
    config = {
        "filesystem_isolation": {
            "profiles": {
                "isolated": {
                    "backend": "docker",
                    "image": "example@sha256:abc",
                    "default_workdir": "/workspace",
                    "allowed_toolsets": ["terminal"],
                    "runtime_identity": identity,
                }
            }
        }
    }

    with pytest.raises(ValueError, match="runtime_identity"):
        parse_execution_profiles(config)


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


def test_unbounded_parent_resolves_valid_host_path_as_bounded_child_scope(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    registry = BackingObjectRegistry()

    scope = resolve_invocation_scope(
        _policy(visible_objects=None),
        "isolated",
        None,
        [{"path": str(selected), "mode": "ro"}],
        backing_registry=registry,
    )

    assert scope is not None
    assert scope.visible_objects == (
        VisibleObjectGrant(
            visible_path=str(selected),
            mode=AccessMode.RO,
            backing=scope.visible_objects[0].backing,
            object_type="directory",
        ),
    )
    record = registry.get(scope.visible_objects[0].backing.object_id)
    assert record is not None
    assert record.backing == scope.visible_objects[0].backing
    assert record.trusted_host_path is True


def test_unbounded_parent_resolves_valid_host_file_as_bounded_child_scope(tmp_path):
    selected = tmp_path / "selected.txt"
    selected.write_text("selected", encoding="utf-8")
    registry = BackingObjectRegistry()

    scope = resolve_invocation_scope(
        _policy(visible_objects=None),
        "isolated",
        None,
        [{"path": str(selected), "mode": "ro"}],
        backing_registry=registry,
    )

    assert scope is not None
    assert scope.visible_objects[0].object_type == "file"
    assert registry.get(scope.visible_objects[0].backing.object_id) is not None


def test_unbounded_parent_rejects_invalid_host_path(tmp_path):
    missing = tmp_path / "missing"

    with pytest.raises(ValueError, match="unavailable"):
        resolve_invocation_scope(
            _policy(visible_objects=None),
            "isolated",
            None,
            [{"path": str(missing), "mode": "ro"}],
            backing_registry=BackingObjectRegistry(),
        )


def test_rejected_unbounded_multi_reveal_does_not_publish_partial_backing(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    missing = tmp_path / "missing"
    registry = BackingObjectRegistry()

    with pytest.raises(ValueError, match="unavailable"):
        resolve_invocation_scope(
            _policy(visible_objects=None),
            "isolated",
            None,
            [
                {"path": str(selected), "mode": "ro"},
                {"path": str(missing), "mode": "ro"},
            ],
            backing_registry=registry,
        )

    selected_object_id = "unbounded_host_" + hashlib.sha256(
        f"{selected}\0directory".encode("utf-8")
    ).hexdigest()
    assert registry.get(selected_object_id) is None


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
        "/var/run/docker.sock",
    ],
)
def test_reveal_applies_operator_selected_exact_grant_without_path_classification(path):
    grant = _grant(path)

    scope = resolve_invocation_scope(
        _policy(visible_objects=(grant,)),
        "isolated",
        None,
        [{"path": path, "mode": "ro"}],
    )

    assert scope is not None
    assert len(scope.visible_objects) == 1
    selected = scope.visible_objects[0]
    assert selected.visible_path == grant.visible_path
    assert selected.mode is AccessMode.RO
    assert selected.backing == grant.backing
    assert selected.object_type == grant.object_type


def test_reveal_rejects_operator_configured_protected_prefix():
    grant = _grant("/protected/child")
    with pytest.raises(ValueError, match="protected"):
        resolve_invocation_scope(
            _policy(visible_objects=(grant,)),
            "isolated",
            None,
            [{"path": "/protected/child", "mode": "ro"}],
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
