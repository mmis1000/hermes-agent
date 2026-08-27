import subprocess

import pytest

from agent.delegation_policy import AccessMode, BackingObjectRef, VisibleObjectGrant
from tools.idmapped_mounts import prepare_idmapped_reveals


def _grant(source, *, object_type="directory", object_id="shared"):
    return VisibleObjectGrant(
        visible_path=f"/workspace/{object_id}",
        mode=AccessMode.RW,
        backing=BackingObjectRef(object_id, "host_path", str(source), "rev-1"),
        object_type=object_type,
    )


def test_prepare_uses_parent_identity_and_cleanup_unmounts(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    staging = tmp_path / "staging"
    calls = []

    monkeypatch.setattr("tools.idmapped_mounts.os.getuid", lambda: 111)
    monkeypatch.setattr("tools.idmapped_mounts.os.getgid", lambda: 222)
    def make_staging(**_kwargs):
        staging.mkdir()
        return str(staging)

    monkeypatch.setattr("tools.idmapped_mounts.tempfile.mkdtemp", make_staging)

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("tools.idmapped_mounts.subprocess.run", run)

    cleanups = []
    prepared = prepare_idmapped_reveals(
        "attempt-1",
        (_grant(source),),
        (10001, 10002),
        register_cleanup=cleanups.append,
    )

    target = staging / "0"
    assert prepared == {"shared": str(target)}
    assert calls == [
        (
            [
                "sudo",
                "-n",
                "--",
                "/usr/bin/mount",
                "--bind",
                "-o",
                "X-mount.idmap=u:111:10001:1 g:222:10002:1",
                str(source),
                str(target),
            ],
            {"check": True, "capture_output": True, "text": True},
        )
    ]

    cleanups[0]()

    assert calls[-1] == (
        ["sudo", "-n", "--", "/usr/bin/umount", str(target)],
        {"check": True, "capture_output": True, "text": True},
    )
    assert not staging.exists()


def test_prepare_maps_regular_file_and_cleanup_unlinks_mountpoint(tmp_path, monkeypatch):
    source = tmp_path / "file"
    source.write_text("data")
    staging = tmp_path / "file-staging"
    calls = []
    cleanups = []

    monkeypatch.setattr("tools.idmapped_mounts.os.getuid", lambda: 111)
    monkeypatch.setattr("tools.idmapped_mounts.os.getgid", lambda: 222)

    def make_staging(**_kwargs):
        staging.mkdir()
        return str(staging)

    monkeypatch.setattr("tools.idmapped_mounts.tempfile.mkdtemp", make_staging)

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[3] == "/usr/bin/mount":
            target = staging / "0"
            assert target.is_file()
            assert not target.is_dir()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("tools.idmapped_mounts.subprocess.run", run)

    prepared = prepare_idmapped_reveals(
        "attempt-file",
        (_grant(source, object_type="file"),),
        (10001, 10002),
        register_cleanup=cleanups.append,
    )

    target = staging / "0"
    assert prepared == {"shared": str(target)}
    assert calls[0][0] == [
        "sudo",
        "-n",
        "--",
        "/usr/bin/mount",
        "--bind",
        "-o",
        "X-mount.idmap=u:111:10001:1 g:222:10002:1",
        str(source),
        str(target),
    ]

    cleanups[0]()

    assert calls[-1][0] == ["sudo", "-n", "--", "/usr/bin/umount", str(target)]
    assert not staging.exists()


def test_partial_setup_cleanup_failure_remains_retryable(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    staging = tmp_path / "partial-staging"
    cleanups = []
    unmount_may_succeed = False

    def make_staging(**_kwargs):
        staging.mkdir()
        return str(staging)

    monkeypatch.setattr("tools.idmapped_mounts.tempfile.mkdtemp", make_staging)

    def run(argv, **_kwargs):
        nonlocal unmount_may_succeed
        if argv[3] == "/usr/bin/mount" and argv[-2] == str(second):
            raise subprocess.CalledProcessError(1, argv, stderr="second mount failed")
        if argv[3] == "/usr/bin/umount" and not unmount_may_succeed:
            raise subprocess.CalledProcessError(1, argv, stderr="busy")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("tools.idmapped_mounts.subprocess.run", run)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        prepare_idmapped_reveals(
            "attempt-partial",
            (
                _grant(first, object_id="first"),
                _grant(second, object_id="second"),
            ),
            (10001, 10001),
            register_cleanup=cleanups.append,
        )

    assert len(cleanups) == 1
    assert staging.exists()
    unmount_may_succeed = True
    cleanups[0]()
    assert not staging.exists()


def test_cleanup_continues_after_one_unmount_failure_and_surfaces_error(
    tmp_path, monkeypatch
):
    first = tmp_path / "cleanup-first"
    second = tmp_path / "cleanup-second"
    first.mkdir()
    second.mkdir()
    staging = tmp_path / "cleanup-staging"
    cleanups = []
    unmounts = []
    fail_second_target = True

    def make_staging(**_kwargs):
        staging.mkdir()
        return str(staging)

    monkeypatch.setattr("tools.idmapped_mounts.tempfile.mkdtemp", make_staging)

    def run(argv, **_kwargs):
        nonlocal fail_second_target
        if argv[3] == "/usr/bin/umount":
            unmounts.append(argv[-1])
            if argv[-1] == str(staging / "1") and fail_second_target:
                raise subprocess.CalledProcessError(1, argv, stderr="busy")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("tools.idmapped_mounts.subprocess.run", run)
    prepare_idmapped_reveals(
        "attempt-cleanup",
        (
            _grant(first, object_id="cleanup-first"),
            _grant(second, object_id="cleanup-second"),
        ),
        (10001, 10001),
        register_cleanup=cleanups.append,
    )

    with pytest.raises(RuntimeError, match="idmapped reveal cleanup failed"):
        cleanups[0]()

    assert unmounts == [str(staging / "1"), str(staging / "0")]
    assert staging.exists()
    fail_second_target = False
    cleanups[0]()
    assert not staging.exists()
