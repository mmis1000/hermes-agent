"""Strict, side-effect-free resolution for delegated filesystem scopes."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import logging
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence
import threading
import uuid

from agent.delegation_policy import (
    AccessMode,
    BackingObjectRef,
    DelegationSessionPolicy,
    ExecutionProfile,
    VisibleObjectGrant,
    attenuate_mode,
    normalize_visible_path,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RevealRequest:
    path: PurePosixPath
    mode: str


@dataclass(frozen=True)
class ResolvedInvocationScope:
    profile_name: str
    profile_hash: str
    profile: ExecutionProfile
    workdir: PurePosixPath
    reveal: tuple[RevealRequest, ...]
    visible_objects: tuple[VisibleObjectGrant, ...]
    profile_template_hash: str | None = None


@dataclass(frozen=True)
class BackingObjectRecord:
    backing: BackingObjectRef
    object_type: str
    exists: bool = True
    root_symlink: bool = False


class BackingObjectRegistry:
    """Read-only backing-object validation interface used during preflight."""

    def __init__(self, records: Mapping[str, Any] | None = None) -> None:
        self._records = dict(records or {})

    def get(self, object_id: str) -> BackingObjectRecord | None:
        record = self._records.get(object_id)
        return record if isinstance(record, BackingObjectRecord) else None


@dataclass
class AttemptResourceLedger:
    cleanup_callbacks: dict[str, Callable[[], None]] = field(default_factory=dict)
    cleaned_resources: list[str] = field(default_factory=list)


@dataclass
class ResolvedAttemptAuthority:
    attempt_id: str
    scope_id: str
    logical_child_id: str
    invocation_scope: ResolvedInvocationScope
    state: str = "starting"
    delegation_id: str | None = None
    run_id: str | None = None
    resources: AttemptResourceLedger = field(default_factory=AttemptResourceLedger)
    backing_registry: Any = None
    prepared_mount_sources: dict[str, str] = field(default_factory=dict)
    task_environment_cleaned: bool = True


class AttemptScopeRegistry:
    """Runtime authority and physical-attempt resource ownership registry."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, ResolvedAttemptAuthority] = {}

    def reserve(
        self,
        invocation_scope: ResolvedInvocationScope,
        logical_child_id: str,
        *,
        attempt_id: str | None = None,
        backing_registry: Any = None,
    ) -> ResolvedAttemptAuthority:
        physical_id = attempt_id or f"attempt_{uuid.uuid4().hex}"
        with self._lock:
            if physical_id in self._records:
                raise ValueError(f"physical attempt already exists: {physical_id}")
            record = ResolvedAttemptAuthority(
                attempt_id=physical_id,
                scope_id=f"scope_{uuid.uuid4().hex}",
                logical_child_id=logical_child_id,
                invocation_scope=invocation_scope,
                backing_registry=backing_registry,
            )
            self._records[physical_id] = record
            return record

    def get(self, attempt_id: str) -> ResolvedAttemptAuthority | None:
        with self._lock:
            return self._records.get(attempt_id)

    def add_resource(
        self, attempt_id: str, resource_key: str, cleanup: Callable[[], None]
    ) -> None:
        with self._lock:
            record = self._records.get(attempt_id)
            if record is None or record.state not in {"starting", "active"}:
                raise ValueError(f"physical attempt is not resource-owning: {attempt_id}")
            if resource_key in record.resources.cleanup_callbacks:
                raise ValueError(f"resource already registered: {resource_key}")
            if resource_key == "task-environment":
                record.task_environment_cleaned = False
                original_cleanup = cleanup

                def cleanup_task_environment() -> None:
                    original_cleanup()
                    with self._lock:
                        record.task_environment_cleaned = True

                cleanup = cleanup_task_environment
            record.resources.cleanup_callbacks[resource_key] = cleanup

    def prepare_idmapped_reveals(self, attempt_id: str) -> None:
        with self._lock:
            record = self._records.get(attempt_id)
            if record is None or record.state not in {"starting", "active"}:
                raise ValueError(f"physical attempt is not resource-owning: {attempt_id}")
            identity = record.invocation_scope.profile.runtime_identity
            grants = record.invocation_scope.visible_objects
            if identity is None or not grants:
                return
            resource_key = "idmapped-reveals"
            if (
                record.prepared_mount_sources
                or resource_key in record.resources.cleanup_callbacks
            ):
                raise ValueError(f"idmapped reveals already prepared: {attempt_id}")

        from tools.idmapped_mounts import prepare_idmapped_reveals

        def register_cleanup(cleanup: Callable[[], None]) -> None:
            def cleanup_registered_mounts() -> None:
                with self._lock:
                    if not record.task_environment_cleaned:
                        raise RuntimeError(
                            "container cleanup must succeed before idmapped unmount"
                        )
                cleanup()
                with self._lock:
                    record.prepared_mount_sources.clear()

            with self._lock:
                if record.state not in {"starting", "active"}:
                    raise ValueError(
                        f"physical attempt is not resource-owning: {attempt_id}"
                    )
                if resource_key in record.resources.cleanup_callbacks:
                    raise ValueError(f"resource already registered: {resource_key}")
                record.resources.cleanup_callbacks[resource_key] = cleanup_registered_mounts

        prepared = prepare_idmapped_reveals(
            attempt_id,
            grants,
            identity,
            register_cleanup=register_cleanup,
        )
        with self._lock:
            if record.state not in {"starting", "active"}:
                raise ValueError(f"physical attempt is not resource-owning: {attempt_id}")
            record.prepared_mount_sources.update(prepared)

    def activate(
        self,
        attempt_id: str,
        *,
        delegation_id: str | None = None,
        run_id: str | None = None,
    ) -> ResolvedAttemptAuthority:
        with self._lock:
            record = self._records.get(attempt_id)
            if record is None or record.state != "starting":
                raise ValueError(f"physical attempt cannot be activated: {attempt_id}")
            record.delegation_id = delegation_id
            record.run_id = run_id
            record.state = "active"
            return record

    def cleanup(self, attempt_id: str) -> tuple[Exception, ...]:
        with self._lock:
            record = self._records.get(attempt_id)
            if record is None:
                return ()
            if record.state == "cleaned":
                return ()
            record.state = "revoked"
            callbacks = list(record.resources.cleanup_callbacks.items())
            record.resources.cleanup_callbacks.clear()
        cleanup_order = list(reversed(callbacks))
        cleanup_order = [
            item for item in cleanup_order if item[0] != "idmapped-reveals"
        ] + [item for item in cleanup_order if item[0] == "idmapped-reveals"]
        errors: list[Exception] = []
        for resource_key, callback in cleanup_order:
            try:
                callback()
            except Exception as exc:  # cleanup must continue across the ledger
                errors.append(exc)
                with self._lock:
                    record.resources.cleanup_callbacks[resource_key] = callback
            else:
                record.resources.cleaned_resources.append(resource_key)
        with self._lock:
            record.state = "revoked" if errors else "cleaned"
        logger.info(
            "delegation_scope_outcome %s",
            json.dumps(
                {
                    "event": "attempt_cleanup_outcome",
                    "environment_owner": record.attempt_id,
                    "scope_id": record.scope_id,
                    "outcome": {
                        "creation": "authority_reserved",
                        "cleanup": "failed" if errors else "succeeded",
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        return tuple(errors)

    def rollback(self, attempt_ids: Sequence[str]) -> None:
        for attempt_id in reversed(tuple(attempt_ids)):
            self.cleanup(attempt_id)

    def cleanup_all(self) -> tuple[Exception, ...]:
        with self._lock:
            attempt_ids = tuple(self._records)
        errors: list[Exception] = []
        for attempt_id in reversed(attempt_ids):
            errors.extend(self.cleanup(attempt_id))
        return tuple(errors)


attempt_scope_registry = AttemptScopeRegistry()


def _parse_reveal(reveal: Sequence[Mapping[str, str]] | None) -> tuple[RevealRequest, ...]:
    if reveal is None:
        return ()
    if not isinstance(reveal, (list, tuple)):
        raise ValueError("delegate_task: reveal must be an array of {path, mode} objects.")
    parsed: list[RevealRequest] = []
    for index, item in enumerate(reveal):
        if not isinstance(item, Mapping) or set(item) != {"path", "mode"}:
            raise ValueError(
                f"delegate_task: reveal[{index}] must contain exactly path and mode."
            )
        path = item["path"]
        mode = item["mode"]
        if not isinstance(path, str) or not isinstance(mode, str):
            raise ValueError(f"delegate_task: reveal[{index}] path and mode must be strings.")
        try:
            canonical_path = normalize_visible_path(path)
            canonical_mode = AccessMode(mode)
        except ValueError as exc:
            raise ValueError(f"delegate_task: invalid reveal[{index}]: {exc}") from exc
        parsed.append(RevealRequest(canonical_path, canonical_mode.value))
    return tuple(parsed)


def _is_within(path: PurePosixPath, prefix: PurePosixPath) -> bool:
    return path == prefix or prefix in path.parents


def _validate_reveal_destination(
    path: PurePosixPath, protected_prefixes: tuple[PurePosixPath | str, ...]
) -> None:
    if path == PurePosixPath("/var/run/docker.sock"):
        raise ValueError("delegate_task: the Docker socket is forbidden.")
    if _is_within(path, PurePosixPath("/home")) or _is_within(
        path, PurePosixPath("/root")
    ):
        raise ValueError(f"delegate_task: host home path {path} is forbidden.")
    credential_markers = {".env", ".ssh", ".aws", ".gnupg", "credentials"}
    if credential_markers.intersection(path.parts):
        raise ValueError(f"delegate_task: credential path {path} is forbidden.")
    for prefix in protected_prefixes:
        canonical_prefix = normalize_visible_path(prefix)
        if _is_within(path, canonical_prefix):
            raise ValueError(f"delegate_task: reveal path {path} is protected.")


def _validate_workdir(
    raw_workdir: str | PurePosixPath,
    profile: ExecutionProfile,
    visible_objects: Sequence[VisibleObjectGrant],
) -> PurePosixPath:
    try:
        candidate = normalize_visible_path(raw_workdir)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"delegate_task: invalid workdir: {exc}") from exc
    for grant in visible_objects:
        grant_path = normalize_visible_path(grant.visible_path)
        if _is_within(candidate, grant_path):
            if grant.mode is AccessMode.RW and grant.object_type == "directory":
                return candidate
            raise ValueError(
                "delegate_task: workdir cannot use non-writable revealed storage."
            )
    local_roots = (
        normalize_visible_path(profile.default_workdir),
        PurePosixPath("/tmp"),
    )
    if not any(_is_within(candidate, root) for root in local_roots):
        raise ValueError(
            "delegate_task: workdir must be container-local or inside writable revealed storage."
        )
    return candidate


_PROFILE_KEYS = frozenset(
    {
        "backend",
        "image",
        "default_workdir",
        "allowed_toolsets",
        "qualified_mcp_servers",
        "network",
        "cpu",
        "memory_mb",
        "shm_mb",
        "pids_limit",
        "runtime_identity",
    }
)


def _parse_runtime_identity(raw: Any) -> tuple[int, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != {"uid", "gid"}:
        raise ValueError("runtime_identity must contain exactly uid and gid")
    values = (raw["uid"], raw["gid"])
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in values
    ):
        raise ValueError("runtime_identity uid and gid must be non-negative integers")
    return values


def parse_execution_profiles(config: Mapping[str, Any]) -> Mapping[str, ExecutionProfile]:
    isolation = config.get("filesystem_isolation", {})
    profiles = isolation.get("profiles", {}) if isinstance(isolation, Mapping) else {}
    if not isinstance(profiles, Mapping):
        raise ValueError("delegation.filesystem_isolation.profiles must be an object")
    parsed: dict[str, ExecutionProfile] = {}
    for name, raw in sorted(profiles.items()):
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ValueError("execution profile names must be non-empty canonical strings")
        if not isinstance(raw, Mapping):
            raise ValueError(f"execution profile {name!r} must be an object")
        unknown = sorted(set(raw).difference(_PROFILE_KEYS))
        if unknown:
            raise ValueError(f"unknown profile keys for {name!r}: {', '.join(unknown)}")
        required = {"backend", "image", "default_workdir", "allowed_toolsets"}
        missing = sorted(required.difference(raw))
        if missing:
            raise ValueError(f"execution profile {name!r} is missing: {', '.join(missing)}")
        backend = raw["backend"]
        if backend != "docker":
            raise ValueError(f"execution profile {name!r} must use the Docker backend")
        image = raw["image"]
        if not isinstance(image, str) or not image.strip():
            raise ValueError(f"execution profile {name!r} image must be a non-empty string")
        toolsets = raw["allowed_toolsets"]
        mcp_servers = raw.get("qualified_mcp_servers", ())
        if not isinstance(toolsets, (list, tuple, set, frozenset)) or not all(
            isinstance(item, str) and item for item in toolsets
        ):
            raise ValueError(f"execution profile {name!r} allowed_toolsets must be strings")
        if not isinstance(mcp_servers, (list, tuple, set, frozenset)) or not all(
            isinstance(item, str) and item for item in mcp_servers
        ):
            raise ValueError(
                f"execution profile {name!r} qualified_mcp_servers must be strings"
            )
        parsed[name] = ExecutionProfile(
            name=name,
            backend=backend,
            image=image,
            default_workdir=raw["default_workdir"],
            allowed_toolsets=frozenset(toolsets),
            qualified_mcp_servers=frozenset(mcp_servers),
            network=raw.get("network", "inherit"),
            cpu=raw.get("cpu"),
            memory_mb=raw.get("memory_mb"),
            shm_mb=raw.get("shm_mb"),
            pids_limit=raw.get("pids_limit"),
            runtime_identity=_parse_runtime_identity(raw.get("runtime_identity")),
        )
    return MappingProxyType(parsed)


def execution_profile_hash(profile: ExecutionProfile) -> str:
    payload = {
        "name": profile.name,
        "backend": profile.backend,
        "image": profile.image,
        "default_workdir": str(profile.default_workdir),
        "allowed_toolsets": sorted(profile.allowed_toolsets),
        "qualified_mcp_servers": sorted(profile.qualified_mcp_servers),
        "network": profile.network,
        "cpu": profile.cpu,
        "memory_mb": profile.memory_mb,
        "shm_mb": profile.shm_mb,
        "pids_limit": profile.pids_limit,
    }
    if profile.runtime_identity is not None:
        payload["runtime_identity"] = {
            "uid": profile.runtime_identity[0],
            "gid": profile.runtime_identity[1],
        }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_AUTHORITY_VERSION = 1


def _execution_profile_payload(profile: ExecutionProfile) -> dict[str, Any]:
    payload = {
        "name": profile.name,
        "backend": profile.backend,
        "image": profile.image,
        "default_workdir": str(profile.default_workdir),
        "allowed_toolsets": sorted(profile.allowed_toolsets),
        "qualified_mcp_servers": sorted(profile.qualified_mcp_servers),
        "network": profile.network,
        "cpu": profile.cpu,
        "memory_mb": profile.memory_mb,
        "shm_mb": profile.shm_mb,
        "pids_limit": profile.pids_limit,
    }
    if profile.runtime_identity is not None:
        payload["runtime_identity"] = {
            "uid": profile.runtime_identity[0],
            "gid": profile.runtime_identity[1],
        }
    return payload


def serialize_delegation_authority(
    scope: ResolvedInvocationScope,
    *,
    enabled_toolsets: Sequence[str],
    disabled_toolsets: Sequence[str],
    scope_id: str,
    attempt_id: str,
    parent_attempt_id: str | None = None,
) -> dict[str, Any]:
    """Return the immutable, non-secret authority persisted for one child."""

    authority = {
        "version": _AUTHORITY_VERSION,
        "profile": {
            "name": scope.profile_name,
            "hash": scope.profile_hash,
            "template_hash": scope.profile_template_hash or scope.profile_hash,
            "snapshot": _execution_profile_payload(scope.profile),
        },
        "workdir": str(scope.workdir),
        "reveal": [
            {"path": str(request.path), "mode": str(request.mode)}
            for request in scope.reveal
        ],
        "visible_objects": [
            {
                "path": str(grant.visible_path),
                "mode": grant.mode.value,
                "object_type": grant.object_type,
                "backing": {
                    "object_id": grant.backing.object_id,
                    "kind": grant.backing.kind,
                    "identity": grant.backing.identity,
                    "revision": grant.backing.revision,
                },
            }
            for grant in scope.visible_objects
        ],
        "tools": {
            "enabled_toolsets": sorted(set(enabled_toolsets)),
            "disabled_toolsets": sorted(set(disabled_toolsets)),
        },
        "lineage": {
            "scope_id": scope_id,
            "attempt_id": attempt_id,
            "parent_attempt_id": parent_attempt_id,
        },
        "state": {"revoked": False, "cleaned": False},
    }
    immutable = {key: value for key, value in authority.items() if key != "state"}
    canonical = json.dumps(immutable, sort_keys=True, separators=(",", ":"))
    authority["authority_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return authority


def delegation_authority_audit_view(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a credential-free, backing-redacted observability snapshot.

    Durable authority must retain exact backing identity for restart validation;
    logs and status surfaces must not.  This projection keeps the effective
    visible namespace and immutable identifiers while redacting daemon-side
    locations that are not visible to the delegated task.
    """

    profile = raw.get("profile") if isinstance(raw, Mapping) else None
    profile = profile if isinstance(profile, Mapping) else {}
    snapshot = profile.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    lineage = raw.get("lineage") if isinstance(raw, Mapping) else None
    lineage = lineage if isinstance(lineage, Mapping) else {}
    state = raw.get("state") if isinstance(raw, Mapping) else None
    state = state if isinstance(state, Mapping) else {}
    cleanup_outcome = (
        "succeeded"
        if state.get("cleaned") is True
        else "revoked_pending"
        if state.get("revoked") is True
        else "pending"
    )
    visible_objects: list[dict[str, Any]] = []
    raw_objects = raw.get("visible_objects", ()) if isinstance(raw, Mapping) else ()
    if isinstance(raw_objects, Sequence) and not isinstance(raw_objects, (str, bytes)):
        for item in raw_objects:
            if not isinstance(item, Mapping):
                continue
            backing = item.get("backing")
            backing = backing if isinstance(backing, Mapping) else {}
            visible_objects.append({
                "path": item.get("path"),
                "mode": item.get("mode"),
                "object_type": item.get("object_type"),
                "backing": {
                    "object_id": backing.get("object_id"),
                    "kind": backing.get("kind"),
                    "identity": "[REDACTED]",
                    "revision": backing.get("revision"),
                },
            })
    return {
        "profile": {
            "name": profile.get("name"),
            "hash": profile.get("hash"),
            "image": snapshot.get("image"),
        },
        "network_mode": snapshot.get("network"),
        "browser": {
            # Host browser tools are never admitted to protected children.
            # Any browser automation must therefore execute inside the
            # attempt environment through an admitted terminal/code path.
            "location": "attempt_container",
            "mode": "terminal_or_code_only",
            "host_browser_tools_admitted": False,
        },
        "mcp": {
            "location": "external_operator_qualified",
            "qualified_servers": sorted(
                item
                for item in snapshot.get("qualified_mcp_servers", ())
                if isinstance(item, str)
            ),
        },
        "environment": {
            "backend": snapshot.get("backend"),
            "owner": lineage.get("attempt_id"),
            "scope_id": lineage.get("scope_id"),
        },
        "outcome": {
            "creation": "authority_reserved",
            "cleanup": cleanup_outcome,
        },
        "workdir": raw.get("workdir") if isinstance(raw, Mapping) else None,
        "reveal": list(raw.get("reveal", ())) if isinstance(raw, Mapping) else [],
        "visible_objects": visible_objects,
        "tools": dict(raw.get("tools", {})) if isinstance(raw, Mapping) else {},
        "lineage": dict(lineage),
        "state": dict(state),
        "implicit_mounts_suppressed": True,
    }


def log_delegation_authority_event(
    event: str,
    authority: Mapping[str, Any],
) -> dict[str, Any]:
    """Emit one structured, redacted lifecycle event and return its payload."""

    if not isinstance(event, str) or not event.strip():
        raise ValueError("delegation audit event name must be non-empty")
    payload = {
        "event": event.strip(),
        "authority": delegation_authority_audit_view(authority),
    }
    logger.info(
        "delegation_scope_event %s",
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )
    return payload


def deserialize_delegation_authority(
    raw: Mapping[str, Any],
    *,
    backing_registry: BackingObjectRegistry | None,
    expected_policy: DelegationSessionPolicy | None = None,
) -> ResolvedInvocationScope:
    """Validate and restore persisted authority, failing closed on any drift."""

    if not isinstance(raw, Mapping) or raw.get("version") != _AUTHORITY_VERSION:
        raise ValueError("protected authority is missing, malformed, or has an unknown version")
    state = raw.get("state")
    if not isinstance(state, Mapping) or set(state) != {"revoked", "cleaned"}:
        raise ValueError("protected authority state is malformed")
    if state.get("revoked") or state.get("cleaned"):
        raise ValueError("protected authority is revoked or cleaned")
    authority_hash = raw.get("authority_hash")
    immutable = {
        key: value
        for key, value in raw.items()
        if key not in {"state", "authority_hash"}
    }
    canonical = json.dumps(immutable, sort_keys=True, separators=(",", ":"))
    expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if not isinstance(authority_hash, str) or authority_hash != expected_hash:
        raise ValueError("protected authority immutable snapshot changed")
    profile_raw = raw.get("profile")
    if not isinstance(profile_raw, Mapping) or not isinstance(
        profile_raw.get("snapshot"), Mapping
    ):
        raise ValueError("protected authority profile is malformed")
    snapshot = profile_raw["snapshot"]
    try:
        profile = ExecutionProfile(
            name=snapshot["name"],
            backend=snapshot["backend"],
            image=snapshot["image"],
            default_workdir=snapshot["default_workdir"],
            allowed_toolsets=frozenset(snapshot["allowed_toolsets"]),
            qualified_mcp_servers=frozenset(snapshot.get("qualified_mcp_servers", ())),
            network=snapshot.get("network", "inherit"),
            cpu=snapshot.get("cpu"),
            memory_mb=snapshot.get("memory_mb"),
            shm_mb=snapshot.get("shm_mb"),
            pids_limit=snapshot.get("pids_limit"),
            runtime_identity=_parse_runtime_identity(snapshot.get("runtime_identity")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("protected authority profile is malformed") from exc
    profile_hash = execution_profile_hash(profile)
    template_hash = profile_raw.get("template_hash", profile_hash)
    if (
        profile_raw.get("name") != profile.name
        or profile_raw.get("hash") != profile_hash
        or not isinstance(template_hash, str)
        or profile.backend != "docker"
    ):
        raise ValueError("protected authority profile is stale or changed")
    if expected_policy is not None:
        expected_profile = expected_policy.profile_snapshots.get(profile.name)
        policy_profile_matches = (
            expected_profile is not None
            and execution_profile_hash(expected_profile) == template_hash
        )
        if (
            not policy_profile_matches
            and "template_hash" not in profile_raw
            and expected_profile is not None
            and expected_profile.network == "inherit"
            and profile.network in {"none", "full"}
        ):
            policy_profile_matches = execution_profile_hash(
                replace(expected_profile, network=profile.network)
            ) == profile_hash
        if (
            profile.name not in expected_policy.allowed_profiles
            or expected_profile is None
            or not policy_profile_matches
        ):
            raise ValueError("protected authority profile is unknown or disabled")

    reveal_raw = raw.get("reveal")
    objects_raw = raw.get("visible_objects")
    tools_raw = raw.get("tools")
    lineage = raw.get("lineage")
    if not isinstance(reveal_raw, list) or not isinstance(objects_raw, list):
        raise ValueError("protected authority reveal declaration is malformed")
    if not isinstance(tools_raw, Mapping) or not isinstance(lineage, Mapping):
        raise ValueError("protected authority tool or lineage snapshot is malformed")
    if not all(
        isinstance(lineage.get(key), str) and bool(lineage.get(key))
        for key in ("scope_id", "attempt_id")
    ):
        raise ValueError("protected authority lineage is malformed")
    for key in ("enabled_toolsets", "disabled_toolsets"):
        value = tools_raw.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("protected authority tool snapshot is malformed")
    if not set(tools_raw["enabled_toolsets"]).issubset(profile.allowed_toolsets):
        raise ValueError("protected authority tool snapshot exceeds the profile")

    try:
        reveal = tuple(
            RevealRequest(normalize_visible_path(item["path"]), AccessMode(item["mode"]).value)
            for item in reveal_raw
            if isinstance(item, Mapping)
        )
        if len(reveal) != len(reveal_raw):
            raise ValueError
        grants: list[VisibleObjectGrant] = []
        for item in objects_raw:
            if not isinstance(item, Mapping) or not isinstance(item.get("backing"), Mapping):
                raise ValueError
            backing_raw = item["backing"]
            backing = BackingObjectRef(
                object_id=backing_raw["object_id"],
                kind=backing_raw["kind"],
                identity=backing_raw["identity"],
                revision=backing_raw["revision"],
            )
            grant = VisibleObjectGrant(
                visible_path=item["path"],
                mode=AccessMode(item["mode"]),
                backing=backing,
                object_type=item["object_type"],
            )
            if backing_registry is None:
                raise ValueError("protected authority backing registry is unavailable")
            record = backing_registry.get(backing.object_id)
            if (
                record is None
                or not record.exists
                or record.root_symlink
                or record.backing != backing
                or record.object_type != grant.object_type
            ):
                raise ValueError("protected authority backing object is missing or stale")
            grants.append(grant)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("protected authority"):
            raise
        raise ValueError("protected authority reveal declaration is malformed") from exc
    if [(str(item.path), item.mode) for item in reveal] != [
        (str(item.visible_path), item.mode.value) for item in grants
    ]:
        raise ValueError("protected authority reveal and visible-object snapshots changed")
    try:
        workdir = normalize_visible_path(raw["workdir"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("protected authority workdir is malformed") from exc
    return ResolvedInvocationScope(
        profile_name=profile.name,
        profile_hash=profile_hash,
        profile=profile,
        workdir=workdir,
        reveal=reveal,
        visible_objects=tuple(grants),
        profile_template_hash=(
            template_hash if template_hash != profile_hash else None
        ),
    )


def resolve_invocation_scope(
    policy: DelegationSessionPolicy | None,
    profile: str | None,
    workdir: str | None,
    reveal: Sequence[Mapping[str, str]] | None,
    *,
    backing_registry: BackingObjectRegistry | None = None,
    inherited_network: bool | None = None,
) -> ResolvedInvocationScope | None:
    selected = profile.strip() if isinstance(profile, str) else None
    if policy is None:
        if not selected and workdir is None and reveal is None:
            return None
        raise ValueError(
            "delegate_task: profile, workdir, and reveal require an admitted protected profile."
        )
    if policy.profile_required and (
        not selected or selected.lower() in {"none", "default", "shared"}
    ):
        choices = ", ".join(sorted(policy.allowed_profiles))
        raise ValueError(
            "delegate_task: profile is required in this session. "
            "Unprofiled/default delegation is disabled. "
            f"Choose one of: {choices}."
        )
    if not selected:
        if policy.allow_profile_none and workdir is None and reveal is None:
            return None
        raise ValueError("delegate_task: workdir and reveal require an admitted protected profile.")
    if selected not in policy.profile_snapshots:
        raise ValueError(f"delegate_task: unknown execution profile {selected!r}.")
    if selected not in policy.allowed_profiles:
        raise ValueError(f"delegate_task: execution profile {selected!r} is not allowed in this session.")
    selected_profile = policy.profile_snapshots[selected]
    if selected_profile.name != selected:
        raise ValueError(f"delegate_task: stale execution profile snapshot for {selected!r}.")
    if selected_profile.backend != "docker":
        raise ValueError("delegate_task: protected execution profiles require the Docker backend.")
    if inherited_network is not None and not isinstance(inherited_network, bool):
        raise TypeError("delegate_task: inherited network setting must be boolean.")
    profile_template_hash = execution_profile_hash(selected_profile)
    if selected_profile.network == "inherit" and inherited_network is not None:
        selected_profile = replace(
            selected_profile,
            network="full" if inherited_network else "none",
        )
    reveal_requests = _parse_reveal(reveal)
    for index, left in enumerate(reveal_requests):
        for right in reveal_requests[index + 1 :]:
            if left.path == right.path:
                raise ValueError(f"delegate_task: duplicate reveal path {left.path}.")
            if (
                _is_within(left.path, right.path) or _is_within(right.path, left.path)
            ) and left.mode != right.mode:
                raise ValueError(
                    f"delegate_task: overlapping reveal paths {left.path} and {right.path} have conflicting modes."
                )
    grants_by_path = {grant.visible_path: grant for grant in policy.visible_objects}
    resolved_grants: list[VisibleObjectGrant] = []
    for request in reveal_requests:
        _validate_reveal_destination(request.path, policy.protected_prefixes)
        if request.path not in grants_by_path:
            raise ValueError(
                f"delegate_task: reveal path {request.path} is outside parent/session ceiling."
            )
        grant = grants_by_path[request.path]
        attenuate_mode(grant.mode, AccessMode(request.mode))
        if backing_registry is not None:
            record = backing_registry.get(grant.backing.object_id)
            if record is None or not record.exists:
                raise ValueError(
                    f"delegate_task: backing object for {request.path} no longer exists."
                )
            if record.root_symlink:
                raise ValueError(
                    f"delegate_task: backing root for {request.path} is a symlink."
                )
            if record.object_type not in {"file", "directory"}:
                raise ValueError(
                    f"delegate_task: backing object for {request.path} must be a regular file or directory."
                )
            if record.object_type != grant.object_type:
                raise ValueError(
                    f"delegate_task: backing object type changed for {request.path}."
                )
            if record.backing.identity != grant.backing.identity:
                raise ValueError(
                    f"delegate_task: backing identity changed for {request.path}."
                )
            if record.backing.revision != grant.backing.revision:
                raise ValueError(
                    f"delegate_task: backing revision changed for {request.path}."
                )
            if (
                record.backing.object_id != grant.backing.object_id
                or record.backing.kind != grant.backing.kind
            ):
                raise ValueError(
                    f"delegate_task: backing identity changed for {request.path}."
                )
        resolved_grants.append(
            VisibleObjectGrant(
                visible_path=request.path,
                mode=AccessMode(request.mode),
                backing=grant.backing,
                object_type=grant.object_type,
            )
        )
    resolved_workdir = _validate_workdir(
        workdir or selected_profile.default_workdir,
        selected_profile,
        resolved_grants,
    )
    return ResolvedInvocationScope(
        profile_name=selected,
        profile_hash=execution_profile_hash(selected_profile),
        profile=selected_profile,
        workdir=resolved_workdir,
        reveal=reveal_requests,
        visible_objects=tuple(resolved_grants),
        profile_template_hash=(
            profile_template_hash
            if profile_template_hash != execution_profile_hash(selected_profile)
            else None
        ),
    )
