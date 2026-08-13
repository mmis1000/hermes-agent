from pathlib import PurePosixPath

import model_tools
from agent.delegation_policy import (
    DelegationSessionPolicy,
    ExecutionProfile,
    derive_child_policy,
)
from tools.delegate_tool import DELEGATE_TASK_SCHEMA


def _profile(name: str) -> ExecutionProfile:
    return ExecutionProfile(
        name=name,
        backend="docker",
        image=f"example/{name}@sha256:abc",
        default_workdir=PurePosixPath("/workspace"),
        allowed_toolsets=frozenset({"terminal"}),
    )


def _policy(*names: str) -> DelegationSessionPolicy:
    profiles = {name: _profile(name) for name in names}
    return DelegationSessionPolicy(
        profile_required=True,
        allow_profile_none=False,
        allowed_profiles=frozenset(names),
        profile_snapshots=profiles,
        visible_objects=(),
        protected_prefixes=(),
    )


def _delegate_schema(definitions):
    return next(
        item["function"] for item in definitions
        if item["function"]["name"] == "delegate_task"
    )


def setup_function():
    model_tools._clear_tool_defs_cache()


def teardown_function():
    model_tools._clear_tool_defs_cache()


def test_protected_schema_pins_profile_enum_and_required_without_global_mutation():
    static_profile = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["profile"]
    assert "enum" not in static_profile
    assert "profile" not in DELEGATE_TASK_SCHEMA["parameters"]["required"]

    schema = _delegate_schema(
        model_tools.get_tool_definitions(
            enabled_toolsets=["delegation"],
            quiet_mode=True,
            delegation_policy=_policy("zeta", "alpha"),
        )
    )

    assert schema["parameters"]["properties"]["profile"]["enum"] == ["alpha", "zeta"]
    assert "profile" in schema["parameters"]["required"]
    assert "enum" not in static_profile
    assert "profile" not in DELEGATE_TASK_SCHEMA["parameters"]["required"]


def test_ordinary_schema_remains_generic_and_profile_optional():
    schema = _delegate_schema(
        model_tools.get_tool_definitions(
            enabled_toolsets=["delegation"], quiet_mode=True
        )
    )
    assert "enum" not in schema["parameters"]["properties"]["profile"]
    assert "profile" not in schema["parameters"].get("required", [])


def test_policy_fingerprint_isolates_cached_profile_enums_and_reuses_equal_policy():
    alpha_first = _delegate_schema(
        model_tools.get_tool_definitions(
            enabled_toolsets=["delegation"], quiet_mode=True,
            delegation_policy=_policy("alpha"),
        )
    )
    alpha_second = _delegate_schema(
        model_tools.get_tool_definitions(
            enabled_toolsets=["delegation"], quiet_mode=True,
            delegation_policy=_policy("alpha"),
        )
    )
    beta = _delegate_schema(
        model_tools.get_tool_definitions(
            enabled_toolsets=["delegation"], quiet_mode=True,
            delegation_policy=_policy("beta"),
        )
    )

    assert alpha_first["parameters"]["properties"]["profile"]["enum"] == ["alpha"]
    assert alpha_second["parameters"]["properties"]["profile"]["enum"] == ["alpha"]
    assert beta["parameters"]["properties"]["profile"]["enum"] == ["beta"]
    assert len(model_tools._tool_defs_cache) == 2


def test_aiagent_pins_policy_before_building_its_tool_schema():
    from run_agent import AIAgent

    policy = _policy("isolated")
    agent = AIAgent(
        model="test/model",
        api_key="test-key",
        base_url="http://localhost:1234/v1",
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
        enabled_toolsets=["delegation"],
        delegation_policy=policy,
    )

    assert agent.delegation_policy is policy
    schema = _delegate_schema(agent.tools)
    assert schema["parameters"]["properties"]["profile"]["enum"] == ["isolated"]


def test_protected_child_schema_is_a_copied_view_of_attenuated_profiles():
    parent = _policy("isolated", "omitted")
    child = derive_child_policy(parent, (), allowed_profiles={"isolated"})
    static_profile = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["profile"]

    schema = _delegate_schema(
        model_tools.get_tool_definitions(
            enabled_toolsets=["delegation"],
            quiet_mode=True,
            delegation_policy=child,
        )
    )

    assert schema["parameters"]["properties"]["profile"]["enum"] == ["isolated"]
    assert schema["parameters"]["properties"]["profile"] is not static_profile
    assert "enum" not in static_profile
