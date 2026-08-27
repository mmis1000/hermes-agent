"""Attempt-owned native idmapped views for protected host-path reveals."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
from typing import Callable, Sequence

from agent.delegation_policy import VisibleObjectGrant


def _run(argv: list[str]) -> None:
    try:
        subprocess.run(argv, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"idmapped mount command failed: {detail}") from exc


def prepare_idmapped_reveals(
    attempt_id: str,
    grants: Sequence[VisibleObjectGrant],
    runtime_identity: tuple[int, int],
    *,
    register_cleanup: Callable[[Callable[[], None]], None],
) -> dict[str, str]:
    """Prepare one private type-preserving mapped path per grant."""

    for grant in grants:
        if grant.backing.kind != "host_path" or grant.object_type not in {
            "file",
            "directory",
        }:
            raise ValueError(
                "runtime_identity supports regular host-path files and directories only"
            )
        source = Path(grant.backing.identity)
        type_matches = (
            source.is_file() if grant.object_type == "file" else source.is_dir()
        )
        if source.is_symlink() or not type_matches:
            raise ValueError(
                "runtime_identity backing type changed: "
                f"{grant.backing.object_id}"
            )

    host_uid = os.getuid()
    host_gid = os.getgid()
    target_uid, target_gid = runtime_identity
    staging_root = Path(tempfile.mkdtemp(prefix=f"hermes-idmap-{attempt_id}-"))
    mounted: list[tuple[Path, str]] = []
    prepared: dict[str, str] = {}

    def cleanup() -> None:
        if not staging_root.exists():
            return
        errors: list[Exception] = []
        for target, object_type in reversed(tuple(mounted)):
            try:
                _run(["sudo", "-n", "--", "/usr/bin/umount", str(target)])
            except Exception as exc:
                errors.append(exc)
            else:
                mounted.remove((target, object_type))
                if object_type == "file":
                    target.unlink()
                else:
                    target.rmdir()
        if errors:
            raise RuntimeError(
                "idmapped reveal cleanup failed: "
                + "; ".join(str(error) for error in errors)
            )
        for child in tuple(staging_root.iterdir()):
            if child.is_dir():
                child.rmdir()
            else:
                child.unlink()
        staging_root.rmdir()

    try:
        register_cleanup(cleanup)
        mapping = (
            f"X-mount.idmap=u:{host_uid}:{target_uid}:1 "
            f"g:{host_gid}:{target_gid}:1"
        )
        for index, grant in enumerate(grants):
            target = staging_root / str(index)
            if grant.object_type == "file":
                target.touch()
            else:
                target.mkdir()
            _run(
                [
                    "sudo",
                    "-n",
                    "--",
                    "/usr/bin/mount",
                    "--bind",
                    "-o",
                    mapping,
                    grant.backing.identity,
                    str(target),
                ]
            )
            mounted.append((target, grant.object_type))
            prepared[grant.backing.object_id] = str(target)
    except Exception as setup_error:
        try:
            cleanup()
        except Exception as cleanup_error:
            raise RuntimeError(
                f"idmapped reveal setup failed: {setup_error}; {cleanup_error}"
            ) from setup_error
        raise

    return prepared
