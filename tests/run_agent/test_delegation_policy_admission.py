from pathlib import Path

import pytest

from run_agent import AIAgent


_PROFILE_CONFIG = """\
delegation:
  filesystem_isolation:
    enabled: true
    allowed_profiles:
      - filesystem-isolated
    profiles:
      filesystem-isolated:
        backend: docker
        image: hermes-filesystem-isolated:test
        default_workdir: /workspace
        allowed_toolsets: [delegation, terminal, file]
        qualified_mcp_servers: []
        network: none
"""


def _write_config(home: Path, content: str = _PROFILE_CONFIG) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(content, encoding="utf-8")


def _construct_standard_agent(**overrides):
    kwargs = dict(
        api_key="test-key",
        base_url="http://127.0.0.1:9/v1",
        model="test-model",
        enabled_toolsets=["delegation"],
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    kwargs.update(overrides)
    return AIAgent(**kwargs)  # type: ignore[arg-type]


def _delegate_schema(agent):
    return next(
        tool["function"] for tool in agent.tools
        if tool["function"]["name"] == "delegate_task"
    )


def test_standard_agent_admits_enabled_profiles_without_mutating_ordinary_schema(
    tmp_path, monkeypatch
):
    from model_tools import get_tool_definitions

    home = tmp_path / "hermes"
    _write_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))

    agent = _construct_standard_agent()

    policy = getattr(agent, "delegation_policy", None)
    assert policy is not None
    assert policy.profile_required is True
    assert policy.allow_profile_none is False
    assert policy.allowed_profiles == frozenset({"filesystem-isolated"})
    assert policy.visible_objects == ()
    assert getattr(agent, "delegation_backing_registry", None) is None
    schema = _delegate_schema(agent)["parameters"]
    assert schema["properties"]["profile"]["enum"] == ["filesystem-isolated"]
    assert "profile" in schema["required"]

    ordinary = next(
        tool["function"]
        for tool in get_tool_definitions(
            enabled_toolsets=["delegation"], quiet_mode=True,
            delegation_policy=None,
        )
        if tool["function"]["name"] == "delegate_task"
    )["parameters"]
    assert "enum" not in ordinary["properties"]["profile"]
    assert "profile" not in ordinary.get("required", ())


@pytest.mark.parametrize(
    "config",
    [
        "model: test-model\n",
        """\
delegation:
  filesystem_isolation:
    enabled: false
    profiles: not-validated-while-disabled
""",
    ],
)
def test_standard_agent_keeps_ordinary_delegation_when_isolation_is_not_enabled(
    tmp_path, monkeypatch, config
):
    from tools.delegation_scope import resolve_invocation_scope

    home = tmp_path / "hermes"
    _write_config(home, config)
    monkeypatch.setenv("HERMES_HOME", str(home))

    agent = _construct_standard_agent()

    assert getattr(agent, "delegation_policy", None) is None
    schema = _delegate_schema(agent)["parameters"]
    assert "profile" not in schema.get("required", ())
    assert "enum" not in schema["properties"]["profile"]
    assert resolve_invocation_scope(
        None,
        profile=None,
        workdir=None,
        reveal=None,
        backing_registry=None,
    ) is None


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            _PROFILE_CONFIG.replace(
                "      - filesystem-isolated\n", "      - missing-profile\n"
            ),
            "unknown profiles: missing-profile",
        ),
        (
            _PROFILE_CONFIG.replace(
                "    allowed_profiles:\n      - filesystem-isolated\n",
                "    allowed_profiles: []\n",
            ),
            "allowed_profiles must be a non-empty list",
        ),
        (
            _PROFILE_CONFIG.replace(
                "    allowed_profiles:\n      - filesystem-isolated\n",
                "    allowed_profiles: filesystem-isolated\n",
            ),
            "allowed_profiles must be a non-empty list",
        ),
        (
            _PROFILE_CONFIG.replace("    enabled: true\n", "    enabled: 'true'\n"),
            "enabled must be a boolean",
        ),
        (
            "delegation:\n  filesystem_isolation: true\n",
            "filesystem_isolation must be a mapping",
        ),
    ],
)
def test_standard_agent_rejects_malformed_enabled_admission_config(
    tmp_path, monkeypatch, config, message
):
    home = tmp_path / "hermes"
    _write_config(home, config)
    monkeypatch.setenv("HERMES_HOME", str(home))

    with pytest.raises(ValueError, match=message):
        _construct_standard_agent()


def test_standard_agent_preserves_explicit_trusted_delegation_policy(
    tmp_path, monkeypatch
):
    from agent.delegation_policy import DelegationSessionPolicy, ExecutionProfile

    configured_home = tmp_path / "hermes"
    _write_config(configured_home)
    monkeypatch.setenv("HERMES_HOME", str(configured_home))
    explicit_profile = ExecutionProfile(
        name="trusted-explicit",
        backend="docker",
        image="trusted-explicit:test",
        default_workdir="/workspace",
        allowed_toolsets={"delegation", "terminal"},
        network="none",
    )
    explicit_policy = DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles={explicit_profile.name},
        profile_snapshots={explicit_profile.name: explicit_profile},
        visible_objects=(),
        protected_prefixes=(),
    )

    agent = _construct_standard_agent(delegation_policy=explicit_policy)

    assert getattr(agent, "delegation_policy", None) is explicit_policy
    schema = _delegate_schema(agent)["parameters"]
    assert schema["properties"]["profile"]["enum"] == ["trusted-explicit"]


@pytest.mark.parametrize("entrypoint", ["cli", "gateway"])
def test_cli_and_gateway_agent_construction_share_profile_admission(
    tmp_path, monkeypatch, entrypoint
):
    home = tmp_path / "hermes"
    _write_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    if entrypoint == "cli":
        from cli import AIAgent as construct_agent

        agent = construct_agent(
            api_key="test-key",
            base_url="http://127.0.0.1:9/v1",
            model="test-model",
            enabled_toolsets=["delegation"],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform="cli",
        )
    else:
        from gateway.config import PlatformConfig
        from gateway.platforms.api_server import APIServerAdapter

        monkeypatch.setattr(
            "gateway.run._resolve_runtime_agent_kwargs",
            lambda: {
                "api_key": "test-key",
                "base_url": "http://127.0.0.1:9/v1",
            },
        )
        monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "test-model")
        monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
        monkeypatch.setattr(
            "gateway.run.GatewayRunner._load_reasoning_config",
            staticmethod(lambda: {}),
        )
        monkeypatch.setattr(
            "gateway.run.GatewayRunner._load_fallback_model",
            staticmethod(lambda: None),
        )
        monkeypatch.setattr("gateway.run._current_max_iterations", lambda: 4)
        monkeypatch.setattr(
            "hermes_cli.tools_config._get_platform_tools",
            lambda *_args: {"delegation"},
        )
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)
        agent = adapter._create_agent(session_id="api-session")

    policy = getattr(agent, "delegation_policy", None)
    assert policy is not None
    assert policy.allowed_profiles == frozenset({"filesystem-isolated"})
    assert _delegate_schema(agent)["parameters"]["properties"]["profile"][
        "enum"
    ] == ["filesystem-isolated"]


def test_standard_admission_is_profile_home_local_and_session_static(
    tmp_path, monkeypatch
):
    cli_home = tmp_path / "cli-profile"
    gateway_home = tmp_path / "gateway-profile"
    _write_config(
        cli_home,
        _PROFILE_CONFIG.replace("filesystem-isolated", "cli-isolated"),
    )
    _write_config(
        gateway_home,
        _PROFILE_CONFIG.replace("filesystem-isolated", "gateway-isolated"),
    )

    monkeypatch.setenv("HERMES_HOME", str(cli_home))
    cli_agent = _construct_standard_agent(platform="cli")
    cli_policy = getattr(cli_agent, "delegation_policy", None)

    monkeypatch.setenv("HERMES_HOME", str(gateway_home))
    gateway_agent = _construct_standard_agent(platform="api_server")
    gateway_policy = getattr(gateway_agent, "delegation_policy", None)

    assert cli_policy is not None
    assert gateway_policy is not None
    assert cli_policy.allowed_profiles == frozenset({"cli-isolated"})
    assert gateway_policy.allowed_profiles == frozenset({"gateway-isolated"})
    assert getattr(cli_agent, "delegation_policy", None) is cli_policy
    assert _delegate_schema(cli_agent)["parameters"]["properties"]["profile"][
        "enum"
    ] == ["cli-isolated"]
    assert _delegate_schema(gateway_agent)["parameters"]["properties"]["profile"][
        "enum"
    ] == ["gateway-isolated"]
