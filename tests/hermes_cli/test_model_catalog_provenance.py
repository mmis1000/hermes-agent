"""Focused provenance checks for delegation admission catalog lookups."""

from unittest.mock import patch

import hermes_cli.models as models


def test_forced_static_fallback_cannot_admit_unverified_model():
    """A failed live resolver returning curated fallback IDs is not verified."""
    with (
        patch.object(models, "_load_provider_models_cache", return_value={}),
        patch.object(models, "_credential_fingerprint", return_value="fp"),
        patch.object(
            models,
            "provider_model_ids",
            return_value=models.ProviderModelCatalog(
                ["gpt-5.6-luna"], verified_models=()
            ),
        ),
    ):
        assert models.cached_provider_model_ids(
            "openai-codex",
            force_refresh=True,
            require_verified=True,
        ) == []


def test_verified_live_ids_are_persisted_and_reused_by_cache_first_lookup():
    saved = {}
    live_catalog = models.ProviderModelCatalog(
        ["gpt-5.6-luna", "curated-only"],
        verified_models=["gpt-5.6-luna"],
    )

    with (
        patch.object(models, "_load_provider_models_cache", return_value={}),
        patch.object(models, "_credential_fingerprint", return_value="fp"),
        patch.object(models, "provider_model_ids", return_value=live_catalog),
        patch.object(models, "_save_provider_models_cache", side_effect=saved.update),
    ):
        assert models.cached_provider_model_ids(
            "openai-codex",
            force_refresh=True,
            require_verified=True,
        ) == ["gpt-5.6-luna"]

    entry = saved["openai-codex"]
    assert entry["verified_models"] == ["gpt-5.6-luna"]

    with (
        patch.object(
            models,
            "_load_provider_models_cache",
            return_value={"openai-codex": entry},
        ),
        patch.object(models, "_credential_fingerprint", return_value="fp"),
        patch.object(models, "provider_model_ids") as live,
    ):
        assert models.cached_provider_model_ids(
            "openai-codex",
            cache_only=True,
            require_verified=True,
        ) == ["gpt-5.6-luna"]
        live.assert_not_called()


def test_route_identity_scopes_cache_and_live_catalog_inputs():
    """Different resolved accounts/endpoints cannot share admission catalog state."""
    cache = {}
    live_catalog = models.ProviderModelCatalog(
        ["route-model"], verified_models=["route-model"]
    )

    def load_cache():
        return dict(cache)

    def save_cache(value):
        cache.clear()
        cache.update(value)

    with (
        patch.object(models, "_load_provider_models_cache", side_effect=load_cache),
        patch.object(models, "_save_provider_models_cache", side_effect=save_cache),
        patch.object(models, "provider_model_ids", return_value=live_catalog) as live,
    ):
        assert models.cached_provider_model_ids(
            "route-provider",
            force_refresh=True,
            require_verified=True,
            api_key="route-a-token",
            base_url="https://route-a.invalid/v1",
            api_mode="chat_completions",
        ) == ["route-model"]
        assert models.cached_provider_model_ids(
            "route-provider",
            cache_only=True,
            require_verified=True,
            api_key="route-b-token",
            base_url="https://route-b.invalid/v1",
            api_mode="chat_completions",
        ) == []
        assert models.cached_provider_model_ids(
            "route-provider",
            force_refresh=True,
            require_verified=True,
            api_key="route-b-token",
            base_url="https://route-b.invalid/v1",
            api_mode="chat_completions",
        ) == ["route-model"]

    assert live.call_count == 2
    assert live.call_args_list[1].kwargs["api_key"] == "route-b-token"
    assert live.call_args_list[1].kwargs["base_url"] == "https://route-b.invalid/v1"


def test_live_provider_discovery_uses_selected_route_not_ambient_openai_config():
    with (
        patch.object(models, "fetch_api_models", return_value=["route-model"]) as fetch,
        patch("hermes_cli.providers.is_official_openai_host", return_value=False),
    ):
        result = models.provider_model_ids(
            "openai",
            api_key="selected-route-token",
            base_url="https://selected-route.invalid/v1",
            api_mode="chat_completions",
        )

    assert "route-model" in result
    fetch.assert_called_once_with(
        "selected-route-token",
        "https://selected-route.invalid/v1",
        api_mode="chat_completions",
    )
