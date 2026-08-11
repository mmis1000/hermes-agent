"""Immutable value objects for protected delegation filesystem policy.

This module intentionally contains no Docker, config, or registry access. Runtime
resolution and resource ownership live in :mod:`tools.delegation_scope`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal, Mapping


class AccessMode(str, Enum):
    """Maximum or requested access for one visible object."""

    RO = "ro"
    RW = "rw"


def attenuate_mode(parent: AccessMode, requested: AccessMode) -> AccessMode:
    """Return the requested mode when it does not exceed the parent ceiling."""

    parent = AccessMode(parent)
    requested = AccessMode(requested)
    if parent is AccessMode.RO and requested is AccessMode.RW:
        raise ValueError("delegated access cannot widen ro to rw")
    return requested


def normalize_visible_path(path: str | PurePosixPath) -> PurePosixPath:
    """Return a canonical absolute POSIX path used on both sides of a reveal."""

    raw = str(path)
    candidate = PurePosixPath(raw)
    if (
        not candidate.is_absolute()
        or candidate == PurePosixPath("/")
        or ".." in candidate.parts
        or raw != str(candidate)
    ):
        raise ValueError(f"visible path must be canonical absolute POSIX path: {raw!r}")
    return candidate


@dataclass(frozen=True)
class BackingObjectRef:
    object_id: str
    kind: Literal["host_path", "named_volume"]
    identity: str
    revision: str


@dataclass(frozen=True)
class VisibleObjectGrant:
    visible_path: PurePosixPath | str
    mode: AccessMode
    backing: BackingObjectRef
    object_type: Literal["file", "directory"]

    def __post_init__(self) -> None:
        object.__setattr__(self, "visible_path", normalize_visible_path(self.visible_path))
        object.__setattr__(self, "mode", AccessMode(self.mode))
        if self.object_type not in {"file", "directory"}:
            raise ValueError(f"unsupported object type: {self.object_type!r}")


@dataclass(frozen=True)
class ExecutionProfile:
    name: str
    backend: str
    image: str
    default_workdir: PurePosixPath | str
    allowed_toolsets: frozenset[str] | set[str] | tuple[str, ...]
    qualified_mcp_servers: frozenset[str] = frozenset()
    network: str = "inherit"
    cpu: float | None = None
    memory_mb: int | None = None
    shm_mb: int | None = None
    pids_limit: int | None = None
    runtime_identity: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "default_workdir", normalize_visible_path(self.default_workdir))
        object.__setattr__(self, "allowed_toolsets", frozenset(self.allowed_toolsets))
        object.__setattr__(self, "qualified_mcp_servers", frozenset(self.qualified_mcp_servers))
        if self.runtime_identity is not None:
            if (
                not isinstance(self.runtime_identity, tuple)
                or len(self.runtime_identity) != 2
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in self.runtime_identity
                )
            ):
                raise ValueError("runtime_identity must be a non-negative integer UID/GID pair")


@dataclass(frozen=True)
class DelegationSessionPolicy:
    profile_required: bool
    allow_profile_none: bool
    allowed_profiles: frozenset[str] | set[str] | tuple[str, ...]
    profile_snapshots: Mapping[str, ExecutionProfile]
    visible_objects: tuple[VisibleObjectGrant, ...]
    protected_prefixes: tuple[PurePosixPath | str, ...]

    def __post_init__(self) -> None:
        snapshots = MappingProxyType(dict(self.profile_snapshots))
        allowed = frozenset(self.allowed_profiles)
        if not allowed.issubset(snapshots):
            missing = sorted(allowed.difference(snapshots))
            raise ValueError(f"allowed profiles missing snapshots: {missing}")
        grants = tuple(self.visible_objects)
        seen_paths: set[PurePosixPath] = set()
        for grant in grants:
            path = normalize_visible_path(grant.visible_path)
            if path in seen_paths:
                raise ValueError(f"duplicate visible path: {path}")
            seen_paths.add(path)
        object.__setattr__(self, "allowed_profiles", allowed)
        object.__setattr__(self, "profile_snapshots", snapshots)
        object.__setattr__(self, "visible_objects", grants)
        object.__setattr__(
            self,
            "protected_prefixes",
            tuple(normalize_visible_path(path) for path in self.protected_prefixes),
        )


def derive_child_policy(
    parent: DelegationSessionPolicy,
    visible_objects: tuple[VisibleObjectGrant, ...],
    *,
    allowed_profiles: frozenset[str] | set[str] | tuple[str, ...] | None = None,
) -> DelegationSessionPolicy:
    """Derive a nested orchestrator policy without widening parent authority."""

    parent_by_path = {grant.visible_path: grant for grant in parent.visible_objects}
    child_grants: list[VisibleObjectGrant] = []
    for grant in visible_objects:
        ceiling = parent_by_path.get(grant.visible_path)
        if ceiling is None:
            raise ValueError(f"visible object outside parent ceiling: {grant.visible_path}")
        if grant.backing != ceiling.backing or grant.object_type != ceiling.object_type:
            raise ValueError(f"visible object identity changed: {grant.visible_path}")
        attenuate_mode(ceiling.mode, grant.mode)
        child_grants.append(grant)

    selected_profiles = frozenset(
        parent.allowed_profiles if allowed_profiles is None else allowed_profiles
    )
    if not selected_profiles.issubset(parent.allowed_profiles):
        raise ValueError("child profiles must be a subset of parent profiles")
    snapshots = {name: parent.profile_snapshots[name] for name in selected_profiles}
    return DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles=selected_profiles,
        profile_snapshots=snapshots,
        visible_objects=tuple(child_grants),
        protected_prefixes=parent.protected_prefixes,
    )
