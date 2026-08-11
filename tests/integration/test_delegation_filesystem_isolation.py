"""Live adversarial gates for protected delegation Docker materialization."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.delegation_policy import DelegationSessionPolicy, ExecutionProfile
from run_agent import AIAgent
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


def test_standard_config_admission_dispatches_private_scratch_container(
    protected_image, tmp_path, monkeypatch
):
    """Exercise standard admission, real delegate preflight, and materialization."""
    import tools.delegate_tool as delegate_tool
    import tools.terminal_tool as terminal_tool

    home = tmp_path / "profile-home"
    home.mkdir()
    host_sentinel = home / "host-only-sentinel"
    host_sentinel.write_text("must-not-be-mounted", encoding="utf-8")
    (home / "config.yaml").write_text(
        f"""\
delegation:
  filesystem_isolation:
    enabled: true
    allowed_profiles:
      - filesystem-isolated
    profiles:
      filesystem-isolated:
        backend: docker
        image: {protected_image}
        default_workdir: /workspace
        allowed_toolsets: [delegation, terminal, file]
        qualified_mcp_servers: []
        network: none
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    parent = AIAgent(
        api_key="test-key",
        base_url="http://127.0.0.1:9/v1",
        model="test-model",
        enabled_toolsets=["delegation"],
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    observation = {}

    def run_conversation(*, task_id, **_kwargs):
        result = json.loads(
            terminal_tool.terminal_tool(
                command=(
                    "set -eu; "
                    "test \"$(id -u)\" -ne 0; "
                    "test \"$(pwd)\" = /workspace; "
                    "test ! -S /var/run/docker.sock; "
                    "test ! -e /root/.hermes; "
                    "test ! -e /home/hermes/.hermes; "
                    "test ! -e /home/hermes/.ssh; "
                    "test ! -e /root/.ssh; "
                    "test ! -e /home/prod; "
                    "printf private-scratch > /workspace/child-only"
                ),
                task_id=task_id,
                timeout=30,
                force=True,
            )
        )
        assert result["exit_code"] == 0, result
        env = terminal_tool._active_environments[task_id]
        container_name = env._container_name
        inspected = json.loads(
            subprocess.run(
                ["docker", "inspect", container_name],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )[0]
        direct = subprocess.run(
            [
                "docker",
                "exec",
                container_name,
                "/bin/sh",
                "-c",
                "id -u; cat /workspace/child-only",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        observation.update(
            task_id=task_id,
            container_name=container_name,
            user=inspected["Config"]["User"],
            mounts=inspected["Mounts"],
            network=inspected["HostConfig"]["NetworkMode"],
            direct=direct,
        )
        return {
            "final_response": "materialized private scratch",
            "completed": True,
            "interrupted": False,
            "api_calls": 0,
            "messages": [],
        }

    def build_child(**kwargs):
        # Child/model execution is replaced only after real invocation preflight.
        scope = kwargs["resolved_scope"]
        assert scope.profile.name == "filesystem-isolated"
        assert scope.visible_objects == ()
        return SimpleNamespace(
            _subagent_id="standard-admission-live-child",
            _delegate_role="leaf",
            _delegate_saved_tool_names=[],
            enabled_toolsets=["terminal", "file"],
            disabled_toolsets=[],
            tool_progress_callback=None,
            run_conversation=run_conversation,
            close=lambda: None,
        )

    monkeypatch.setattr(delegate_tool, "_build_child_agent", build_child)
    output = json.loads(
        delegate_tool.delegate_task(
            goal="materialize admitted scratch",
            profile="filesystem-isolated",
            workdir="/workspace",
            parent_agent=parent,
        )
    )

    assert "results" in output, output
    assert output["results"][0]["status"] == "completed", output
    assert observation["user"] == "hermes"
    assert observation["mounts"] == []
    assert observation["network"] == "none"
    assert observation["direct"] == ["10001", "private-scratch"]
    assert host_sentinel.read_text(encoding="utf-8") == "must-not-be-mounted"
    assert subprocess.run(
        ["docker", "inspect", observation["container_name"]],
        capture_output=True,
    ).returncode != 0


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
