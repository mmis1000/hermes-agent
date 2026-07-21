import subprocess
import pytest

from tools.environments import docker as docker_env


def _capture_docker(monkeypatch):
    calls = []
    docker_env._cgroup_limits_ok = False
    monkeypatch.setattr(docker_env, "_ensure_docker_available", lambda: None)
    monkeypatch.setattr(docker_env, "find_docker", lambda: "docker")
    monkeypatch.setattr(docker_env, "_image_uses_init_entrypoint", lambda *_a: False)

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if len(cmd) > 1 and cmd[1] == "run":
            return subprocess.CompletedProcess(cmd, 0, stdout="container-id\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_env.subprocess, "run", run)
    return calls


def test_trusted_mount_compiles_to_structured_read_only_docker_mount(monkeypatch):
    calls = _capture_docker(monkeypatch)

    docker_env.DockerEnvironment(
        image="repo/protected@sha256:deadbeef",
        cwd="/workspace",
        task_id="attempt-1",
        trusted_mounts=[
            {
                "kind": "host_path",
                "source": "/trusted/backing/file.txt",
                "target": "/visible/file.txt",
                "mode": "ro",
            }
        ],
        suppress_implicit_mounts=True,
        persist_across_processes=False,
    )

    run_argv = next(cmd for cmd in calls if len(cmd) > 1 and cmd[1] == "run")
    mount_index = run_argv.index("--mount")
    assert run_argv[mount_index + 1] == (
        "type=bind,src=/trusted/backing/file.txt,"
        "dst=/visible/file.txt,readonly"
    )


def test_strict_mount_mode_suppresses_all_ambient_docker_authority(monkeypatch):
    from tools import credential_files

    calls = _capture_docker(monkeypatch)
    monkeypatch.setattr(
        credential_files,
        "get_credential_file_mounts",
        lambda: (_ for _ in ()).throw(AssertionError("credential discovery")),
    )
    monkeypatch.setattr(
        credential_files,
        "get_skills_directory_mount",
        lambda: (_ for _ in ()).throw(AssertionError("skill discovery")),
    )
    monkeypatch.setattr(
        credential_files,
        "get_cache_directory_mounts",
        lambda: (_ for _ in ()).throw(AssertionError("cache discovery")),
    )

    docker_env.DockerEnvironment(
        image="repo/protected@sha256:deadbeef",
        cwd="/workspace",
        task_id="attempt-strict",
        volumes=["/ambient:/ambient"],
        forward_env=["HOME"],
        env={"AMBIENT_SECRET": "nope"},
        extra_args=["--privileged"],
        host_cwd="/ambient-host-cwd",
        auto_mount_cwd=True,
        suppress_implicit_mounts=True,
        persist_across_processes=False,
    )

    run_argv = next(cmd for cmd in calls if len(cmd) > 1 and cmd[1] == "run")
    rendered = "\n".join(run_argv)
    assert "/ambient" not in rendered
    assert "AMBIENT_SECRET" not in rendered
    assert "--privileged" not in run_argv


def test_strict_mode_suppresses_global_environment_passthrough(monkeypatch):
    from tools import env_passthrough

    _capture_docker(monkeypatch)
    monkeypatch.setenv("HERMES_STRICT_LEAK_SENTINEL", "must-not-cross")
    monkeypatch.setattr(
        env_passthrough,
        "get_all_passthrough",
        lambda: {"HERMES_STRICT_LEAK_SENTINEL"},
    )

    env = docker_env.DockerEnvironment(
        image="repo/protected@sha256:deadbeef",
        cwd="/workspace",
        task_id="attempt-passthrough",
        suppress_implicit_mounts=True,
        persist_across_processes=False,
    )

    assert not any(
        "HERMES_STRICT_LEAK_SENTINEL" in arg for arg in env._init_env_args
    )


def test_protected_resource_limits_and_identity_labels_are_typed(monkeypatch):
    calls = _capture_docker(monkeypatch)
    docker_env._cgroup_limits_ok = True

    docker_env.DockerEnvironment(
        image="repo/protected@sha256:deadbeef",
        cwd="/workspace",
        task_id="attempt-typed",
        cpu=2.5,
        memory=2048,
        shm_mb=256,
        pids_limit=64,
        delegation_scope_id="scope-typed",
        delegation_attempt_id="attempt-typed",
        suppress_implicit_mounts=True,
        persist_across_processes=False,
    )

    run_argv = next(cmd for cmd in calls if len(cmd) > 1 and cmd[1] == "run")
    assert run_argv[run_argv.index("--shm-size") + 1] == "256m"
    assert run_argv[run_argv.index("--pids-limit") + 1] == "64"
    assert "hermes-delegation-scope-id=scope-typed" in run_argv
    assert "hermes-delegation-attempt-id=attempt-typed" in run_argv


def test_backing_substitution_is_rejected_immediately_before_docker_run(monkeypatch):
    calls = _capture_docker(monkeypatch)

    with pytest.raises(ValueError, match="backing substitution"):
        docker_env.DockerEnvironment(
            image="repo/protected@sha256:deadbeef",
            cwd="/workspace",
            task_id="attempt-race",
            trusted_mounts=[],
            trusted_mounts_validator=lambda: (_ for _ in ()).throw(
                ValueError("backing substitution detected")
            ),
            suppress_implicit_mounts=True,
            persist_across_processes=False,
        )

    assert not any(len(cmd) > 1 and cmd[1] == "run" for cmd in calls)
