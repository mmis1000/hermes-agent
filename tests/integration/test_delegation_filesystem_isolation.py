"""Live adversarial gates for protected delegation Docker materialization."""

import shutil
import subprocess
from pathlib import Path

import pytest

from agent.delegation_policy import DelegationSessionPolicy, ExecutionProfile
from tools.delegation_scope import resolve_invocation_scope
from tools.environments.docker import DockerEnvironment


def _docker_available():
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="Docker daemon unavailable"),
]


@pytest.fixture(scope="module")
def protected_image():
    root = Path(__file__).parents[2]
    image = "hermes-delegation-isolation:test"
    subprocess.run(
        ["docker", "build", "-t", image, str(root / "containers/delegation")],
        check=True,
        timeout=300,
    )
    yield image


def _strict_env(image, task_id, *, mounts=()):
    return DockerEnvironment(
        image=image,
        cwd="/workspace",
        task_id=task_id,
        trusted_mounts=list(mounts),
        suppress_implicit_mounts=True,
        persist_across_processes=False,
        network=False,
        pids_limit=64,
        memory=256,
    )


@pytest.mark.parametrize(
    ("session_network", "docker_mode"), [(False, "none"), (True, "bridge")]
)
def test_live_inherited_network_matches_admitted_session_setting(
    protected_image, session_network, docker_mode
):
    profile = ExecutionProfile(
        name="protected",
        backend="docker",
        image=protected_image,
        default_workdir="/workspace",
        allowed_toolsets={"terminal"},
        network="inherit",
    )
    policy = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles=frozenset({profile.name}),
        profile_snapshots={profile.name: profile},
        visible_objects=(),
        protected_prefixes=(),
    )
    scope = resolve_invocation_scope(
        policy,
        profile.name,
        None,
        None,
        inherited_network=session_network,
    )
    assert scope is not None
    env = DockerEnvironment(
        image=protected_image,
        cwd="/workspace",
        task_id=f"network-inherit-{session_network}",
        suppress_implicit_mounts=True,
        persist_across_processes=False,
        network=scope.profile.network == "full",
    )
    try:
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.HostConfig.NetworkMode}}",
                env._container_name,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert inspected == docker_mode
    finally:
        env.cleanup(force_remove=True)


def test_leaf_reveal_is_readable_but_ancestor_sibling_and_host_authority_are_not(
    protected_image, tmp_path
):
    revealed = tmp_path / "leaf.txt"
    sibling = tmp_path / "sibling.txt"
    revealed.write_text("VISIBLE", encoding="utf-8")
    sibling.write_text("HIDDEN-SIBLING", encoding="utf-8")
    env = _strict_env(
        protected_image,
        "isolation-leaf",
        mounts=[{
            "kind": "host_path",
            "source": str(revealed),
            "target": "/visible/leaf.txt",
            "mode": "ro",
        }],
    )
    try:
        assert env.execute("cat /visible/leaf.txt").get("output", "").strip() == "VISIBLE"
        assert env.execute("printf hacked > /visible/leaf.txt").get("returncode") != 0
        probe = env.execute(
            "test ! -e /visible/sibling.txt && "
            "test ! -S /var/run/docker.sock && "
            "test ! -e /root/.hermes && "
            "test ! -e /home/hermes/.ssh"
        )
        assert probe.get("returncode") == 0, probe
    finally:
        env.cleanup()


def test_parallel_attempts_have_distinct_containers_and_private_roots(protected_image):
    first = _strict_env(protected_image, "isolation-attempt-one")
    second = _strict_env(protected_image, "isolation-attempt-two")
    try:
        assert first._container_id != second._container_id
        assert first.execute("printf first > /workspace/private.txt").get("returncode") == 0
        assert second.execute("test ! -e /workspace/private.txt").get("returncode") == 0
    finally:
        second.cleanup()
        first.cleanup()


def test_strict_environment_does_not_import_global_passthrough(
    protected_image, monkeypatch
):
    from tools import env_passthrough

    sentinel = "HERMES_PROTECTED_LIVE_LEAK_SENTINEL"
    monkeypatch.setenv(sentinel, "must-not-cross")
    monkeypatch.setattr(env_passthrough, "get_all_passthrough", lambda: {sentinel})
    env = _strict_env(protected_image, "isolation-env-leak")
    try:
        probe = env.execute(f'test -z "${{{sentinel}+present}}"')
        assert probe.get("returncode") == 0, probe
    finally:
        env.cleanup(force_remove=True)


def test_protected_force_cleanup_returns_only_after_real_container_removal(
    protected_image,
):
    env = _strict_env(protected_image, "isolation-force-remove")
    container_id = env._container_id
    assert container_id

    env.cleanup(force_remove=True)

    inspect = subprocess.run(
        ["docker", "inspect", container_id],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert inspect.returncode != 0, inspect.stdout + inspect.stderr
