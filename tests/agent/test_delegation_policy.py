from __future__ import annotations

import pytest

from agent.delegation_policy import (
    AccessMode,
    BackingObjectRef,
    DelegationSessionPolicy,
    ExecutionProfile,
    VisibleObjectGrant,
    attenuate_mode,
    derive_child_policy,
    normalize_visible_path,
)


@pytest.mark.parametrize(
    ("parent", "requested"),
    [
        (AccessMode.RW, AccessMode.RW),
        (AccessMode.RW, AccessMode.RO),
        (AccessMode.RO, AccessMode.RO),
    ],
)
def test_access_mode_attenuation_accepts_equal_or_narrower(parent, requested):
    assert attenuate_mode(parent, requested) is requested


def test_access_mode_attenuation_rejects_rw_over_ro():
    with pytest.raises(ValueError, match="cannot widen"):
        attenuate_mode(AccessMode.RO, AccessMode.RW)


@pytest.mark.parametrize("value", ["relative/path", "../escape", "/tmp/../secret", "/"])
def test_visible_paths_must_be_canonical_absolute_non_root(value):
    with pytest.raises(ValueError, match="canonical absolute"):
        normalize_visible_path(value)


def test_visible_path_preserves_canonical_identity():
    assert str(normalize_visible_path("/workspace/src")) == "/workspace/src"


def _grant(path="/workspace/src", mode=AccessMode.RW):
    return VisibleObjectGrant(
        visible_path=path,
        mode=mode,
        backing=BackingObjectRef(
            object_id="obj-1",
            kind="host_path",
            identity="dev:1:ino:2",
            revision="rev-1",
        ),
        object_type="directory",
    )


def _profile():
    return ExecutionProfile(
        name="isolated",
        backend="docker",
        image="example@sha256:abc",
        default_workdir="/workspace",
        allowed_toolsets={"terminal", "file"},
    )


def test_policy_snapshots_are_deeply_immutable():
    grant = _grant()
    profile = _profile()
    source = {"isolated": profile}
    policy = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={"isolated"},
        profile_snapshots=source,
        visible_objects=(grant,),
        protected_prefixes=("/root",),
    )

    source["other"] = profile
    assert set(policy.profile_snapshots) == {"isolated"}
    assert policy.allowed_profiles == frozenset({"isolated"})
    assert profile.allowed_toolsets == frozenset({"terminal", "file"})
    assert grant.visible_path.as_posix() == "/workspace/src"
    with pytest.raises(TypeError):
        policy.profile_snapshots["other"] = profile  # type: ignore[index]


def test_child_policy_is_derived_only_from_effective_child_grants():
    root = _grant("/workspace/root", AccessMode.RW)
    sibling = VisibleObjectGrant(
        visible_path="/workspace/sibling",
        mode=AccessMode.RO,
        backing=BackingObjectRef("obj-2", "host_path", "dev:1:ino:3", "rev-1"),
        object_type="directory",
    )
    profile = _profile()
    parent = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={"isolated"},
        profile_snapshots={"isolated": profile},
        visible_objects=(root, sibling),
        protected_prefixes=("/root",),
    )

    child = derive_child_policy(parent, (_grant("/workspace/root", AccessMode.RO),))

    assert [str(grant.visible_path) for grant in child.visible_objects] == ["/workspace/root"]
    assert child.visible_objects[0].mode is AccessMode.RO
    with pytest.raises(ValueError, match="outside parent ceiling"):
        derive_child_policy(parent, (_grant("/workspace/hidden", AccessMode.RO),))


def test_child_policy_from_unbounded_parent_accepts_selected_child_limit():
    profile = _profile()
    parent = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={"isolated"},
        profile_snapshots={"isolated": profile},
        visible_objects=None,
        protected_prefixes=("/root",),
    )
    selected = (_grant("/workspace/selected", AccessMode.RO),)

    child = derive_child_policy(parent, selected)

    assert child.visible_objects == selected


def test_unbounded_parent_can_derive_unbounded_child():
    profile = _profile()
    parent = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={"isolated"},
        profile_snapshots={"isolated": profile},
        visible_objects=None,
        protected_prefixes=("/root",),
    )

    child = derive_child_policy(parent, None)
    selected = (_grant("/workspace/selected", AccessMode.RO),)
    grandchild = derive_child_policy(child, selected)

    assert child.visible_objects is None
    assert grandchild.visible_objects == selected


def test_bounded_parent_cannot_derive_unbounded_child():
    profile = _profile()
    parent = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={"isolated"},
        profile_snapshots={"isolated": profile},
        visible_objects=(),
        protected_prefixes=("/root",),
    )

    with pytest.raises(ValueError, match="unbounded child requires an unbounded parent"):
        derive_child_policy(parent, None)


def test_empty_bounded_parent_still_rejects_selected_child_limit():
    profile = _profile()
    parent = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={"isolated"},
        profile_snapshots={"isolated": profile},
        visible_objects=(),
        protected_prefixes=("/root",),
    )

    with pytest.raises(ValueError, match="outside parent ceiling"):
        derive_child_policy(parent, (_grant("/workspace/selected", AccessMode.RO),))


def test_policy_rejects_duplicate_or_overlapping_conflicting_grants():
    profile = _profile()
    with pytest.raises(ValueError, match="duplicate visible path"):
        DelegationSessionPolicy(
            profile_required=True,
            allow_profile_none=False,
            allowed_profiles={"isolated"},
            profile_snapshots={"isolated": profile},
            visible_objects=(_grant(), _grant()),
            protected_prefixes=("/root",),
        )
