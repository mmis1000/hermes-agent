"""Provider/model catalogs: discovery, caching, and identity helpers.

Origin module; cohesive clusters live in siblings and are re-imported here so
``hermes_cli.models.<name>`` stays the stable import/monkeypatch surface:
``models_catalog_static`` (curated tables, provider registry, aliases), ``models_reasoning_caps``,
``models_local`` (Ollama / LM Studio), ``models_pricing``, ``models_validate``.
"""

from __future__ import annotations

import contextvars
import copy
import gzip
import json
from hermes_cli.models_reasoning_caps import parse_openrouter_reasoning_capabilities
import logging
import os
import re
import sys
import threading
import urllib.parse
import urllib.request
import urllib.error
import time
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from typing import TypeGuard

from hermes_cli.route_identity import normalize_route_base_url
from hermes_cli.urllib_security import open_credentialed_url
from hermes_cli.version_info import get_version_info
from hermes_cli.models_catalog_static import (
    CANONICAL_PROVIDERS,
    OPENROUTER_MODELS,
    PREFERRED_SILENT_DEFAULT_MODEL,
    VERCEL_AI_GATEWAY_MODELS,
    _AGGREGATOR_PROVIDERS,
    _AZURE_FOUNDRY_RESPONSES_PREFIXES,
    _BORROWED_MODEL_PROVIDERS,
    _COPILOT_MODEL_ALIASES,
    _LIVE_FIRST_PICKER_PROVIDERS,
    _MODELS_DEV_PREFERRED,
    _OPENAI_FAST_MODE_PREFIXES,
    _OPENAI_ULTRAFAST_MODELS,
    _PROVIDER_ALIASES,
    _PROVIDER_LABELS,
    _PROVIDER_MODELS,
    _PROVIDER_RETIRED_ALIASES,
    _SILENT_DEFAULT_PROVIDERS,
    _xai_finalize_catalog)
from hermes_cli.models_reasoning_caps import (
    _OPENROUTER_CATALOG_URL,
    _seed_reasoning_caps)
from hermes_cli.models_local import (
    _load_ollama_cloud_cache,
    _ollama_cloud_cache_path,
    _strip_ollama_cloud_suffix,
    _OLLAMA_LOCAL_MODELS_CACHE,
    _OLLAMA_LOCAL_MODELS_CACHE_TTL,
    _OLLAMA_LOCAL_PROBE_FAILURE_CACHE,
    _OLLAMA_LOCAL_PROBE_REACHABLE,
    _get_ollama_base_url,
    _get_ollama_native_headers,
    _ollama_local_catalog,
    _ollama_probe_cache_key,
    _root_for_ollama_native_api,
    fetch_ollama_cloud_models)

logger = logging.getLogger(__name__)

# Identify ourselves so endpoints fronted by Cloudflare's Browser Integrity
# Check (error 1010) don't reject the default ``Python-urllib/*`` signature.
_HERMES_USER_AGENT = f"hermes-cli/{get_version_info().base_version}"

COPILOT_BASE_URL = "https://api.githubcopilot.com"
COPILOT_MODELS_URL = f"{COPILOT_BASE_URL}/models"
COPILOT_EDITOR_VERSION = "vscode/1.104.1"
COPILOT_REASONING_EFFORTS_GPT5 = ["minimal", "low", "medium", "high"]
COPILOT_REASONING_EFFORTS_O_SERIES = ["low", "medium", "high"]

def _urlopen_model_catalog_request(req: urllib.request.Request, *, timeout: float, ssl_context=None):
    """Open catalog requests without forwarding headers across origins."""
    return open_credentialed_url(req, timeout=timeout, ssl_context=ssl_context)


def _get_json(
    url: str, *, timeout: float, headers: Optional[dict[str, str]] = None, opener=None, **open_kwargs: Any
) -> Any:
    """GET ``url`` and parse the JSON body. ``opener`` defaults to the catalog opener (resolved at
    call time so monkeypatching ``_urlopen_model_catalog_request`` still applies). Raises on failure."""
    req = urllib.request.Request(url, headers=headers or {})
    with (opener or _urlopen_model_catalog_request)(req, timeout=timeout, **open_kwargs) as resp:
        body = resp.read()
        if req.get_header("Accept-encoding") == "gzip" and resp.headers.get("Content-Encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
        return json.loads(body.decode())



def _read_json_cache(path: Path, *, errors=Exception) -> Optional[dict]:
    """Load a JSON-object cache file; None when missing, unreadable, or not a dict."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except errors:
        return None
    return data if isinstance(data, dict) else None


def _write_json_cache(path: Path, data: Any, **dump_kwargs: Any) -> None:
    """Atomically persist a cache file (creating parents). Raises on failure — callers decide
    whether a failed cache write is worth logging."""
    from utils import atomic_json_write
    from hermes_constants import mkdir_under_hermes_home

    mkdir_under_hermes_home(path.parent)
    atomic_json_write(path, data, **dump_kwargs)


def _merge_unique(primary: list[str], secondary: list[str], key=lambda m: str(m).lower()) -> list[str]:
    """``primary`` verbatim, then ``secondary`` entries whose ``key`` is new (deduped as it goes)."""
    merged, seen = list(primary), {key(m) for m in primary}
    for m in secondary:
        k = key(m)
        if k not in seen:
            seen.add(k)
            merged.append(m)
    return merged


def _custom_provider_ssl_context(base_url: str):
    """Use the same trust decision for urllib catalogs and HTTPX metadata/chat."""
    from agent.model_metadata_http import resolve_verify

    verify = resolve_verify(base_url)
    if verify is True:
        return None
    if verify is False:
        import ssl

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    return verify


# Process-lifetime picker lists refreshed from the live catalogs (see fetch_*_models).
_openrouter_catalog_cache: list[tuple[str, str]] | None = None

# The in-memory ``_openrouter_catalog_cache`` is per-process, so without a disk cache every cold
# picker open re-downloads the full ~686KB /api/v1/models catalog. The *curated* result
# (post-filter) is persisted under the same TTL the catalog manifest uses, so both layers go
# stale together.


def _openrouter_catalog_disk_ttl() -> float:
    """Same TTL as the catalog manifest this list is filtered from (honours ``model_catalog.ttl_minutes``)."""
    from hermes_cli.model_catalog import refresh_interval_seconds

    return refresh_interval_seconds()


def _openrouter_catalog_disk_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "cache" / "openrouter_curated_catalog.json"


def _read_openrouter_catalog_disk(*, allow_stale: bool = False) -> list[tuple[str, str]] | None:
    """Fresh curated catalog from disk, or None (missing, corrupt, expired, or empty).

    ``allow_stale`` ignores the TTL — the cache-only read path prefers a stale copy over a live GET."""
    obj = _read_json_cache(_openrouter_catalog_disk_path())
    if obj is None:
        return None
    try:
        if not allow_stale and time.time() - float(obj.get("fetched_at", 0)) > _openrouter_catalog_disk_ttl():
            return None
    except (TypeError, ValueError):
        return None
    items = obj.get("curated")
    if not isinstance(items, list):
        return None
    out = [(str(it[0]), str(it[1])) for it in items if isinstance(it, (list, tuple)) and len(it) == 2]
    return out or None


def _write_openrouter_catalog_disk(curated: list[tuple[str, str]]) -> None:
    try:
        _write_json_cache(
            _openrouter_catalog_disk_path(),
            {"fetched_at": time.time(), "curated": [list(c) for c in curated]})
    except Exception as exc:
        logger.debug("openrouter curated catalog disk write failed: %s", exc)
_ai_gateway_catalog_cache: list[tuple[str, str]] | None = None


# ---------------------------------------------------------------------------
# Nous Portal free-model helpers — the Portal models endpoint is the source of truth for what is
# offered (free or paid); we surface it as-is, no local allowlist filtering.
# ---------------------------------------------------------------------------


def _zero_priced(pricing: Any, keys: tuple[str, str], default: str) -> bool:
    """True when both pricing fields parse to 0 (missing fields read as ``default``)."""
    if not isinstance(pricing, dict):
        return False
    try:
        return all(float(pricing.get(k, default)) == 0 for k in keys)
    except (TypeError, ValueError):
        return False


def _is_subscription_billed(entry: Any) -> bool:
    """The gateway bills this catalog row to a subscription the account holds, not to credits."""
    return isinstance(entry, dict) and entry.get("billing_mode") == "subscription"


def _is_model_free(model_id: str, pricing: dict[str, dict[str, str]]) -> bool:
    """Return True if *model_id* costs no credits: zero-cost prompt AND completion pricing, or a row
    the gateway bills to a subscription."""
    entry = pricing.get(model_id)
    return bool(entry) and (_is_subscription_billed(entry) or _zero_priced(entry, ("prompt", "completion"), "1"))


def partition_nous_models_by_tier(
    model_ids: list[str], pricing: dict[str, dict[str, str]], free_tier: bool
) -> tuple[list[str], list[str]]:
    """Split Nous models into (selectable, unavailable): free-tier users may only select free models
    (paid ones are returned as unavailable, shown grayed out)."""
    if not free_tier or not pricing:  # no pricing → can't determine, show everything
        return (model_ids, [])
    selectable = [mid for mid in model_ids if _is_model_free(mid, pricing)]
    return (selectable, [mid for mid in model_ids if mid not in selectable])


def _union_with_portal_recommendations(
    tier_key: str, curated_ids: list[str], pricing: dict[str, dict[str, str]], portal_base_url: str,
    *, force_refresh: bool, synthesize_free_pricing: bool,
) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Append the Portal's ``<tier_key>`` recommendations missing from ``curated_ids``.

    Curated models show first, Portal-only picks follow. Failures (network, parse, missing field)
    silently return the inputs unchanged — never block the picker on a Portal-side hiccup.
    """
    try:
        payload = fetch_nous_recommended_models(portal_base_url, force_refresh=force_refresh)
    except Exception:
        payload = None
    block = payload.get(tier_key) if isinstance(payload, dict) else None
    entries = block if isinstance(block, list) else []
    portal_ids = [name for entry in entries if (name := _extract_model_name(entry))]
    if not portal_ids:
        return (list(curated_ids), dict(pricing))

    augmented_pricing = dict(pricing)
    if synthesize_free_pricing:
        for mid in portal_ids:
            augmented_pricing.setdefault(mid, {"prompt": "0", "completion": "0"})
    seen = set(curated_ids)
    return (list(curated_ids) + [mid for mid in portal_ids if mid not in seen], augmented_pricing)


def union_with_portal_free_recommendations(
    curated_ids: list[str], pricing: dict[str, dict[str, str]], portal_base_url: str = "", *,
    force_refresh: bool = False) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Curated list + pricing plus the Portal's ``freeRecommendedModels``; Portal-only free picks get a
    synthetic $0 pricing entry so tier partitioning sees them as free."""
    return _union_with_portal_recommendations(
        "freeRecommendedModels", curated_ids, pricing, portal_base_url,
        force_refresh=force_refresh, synthesize_free_pricing=True)


def union_with_portal_paid_recommendations(
    curated_ids: list[str], pricing: dict[str, dict[str, str]], portal_base_url: str = "", *,
    force_refresh: bool = False) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Curated list plus the Portal's ``paidRecommendedModels``; ``pricing`` is deliberately left untouched."""
    return _union_with_portal_recommendations(
        "paidRecommendedModels", curated_ids, pricing, portal_base_url,
        force_refresh=force_refresh, synthesize_free_pricing=False)


# Free-tier detection cache, per profile — short so an account upgrade shows within minutes.
_FREE_TIER_CACHE_TTL: int = 180  # seconds
_free_tier_cache: dict[str, tuple[bool, float]] = {}  # profile key -> (result, timestamp)


def _pricing_profile_key() -> str:
    """Stable profile identity for process-local pricing caches."""
    from hermes_constants import hermes_home_key

    return hermes_home_key()


def get_cached_nous_free_tier() -> Optional[bool]:
    """This profile's live cached entitlement, or ``None`` if unknown/expired."""
    cached = _free_tier_cache.get(_pricing_profile_key())
    if cached is None or time.monotonic() - cached[1] >= _FREE_TIER_CACHE_TTL:
        return None
    return cached[0]


def check_nous_free_tier(*, force_fresh: bool = False, cached_only: bool = False) -> bool:
    """True only when the Nous Portal user is KNOWN to be free-tier (unknown/error → False so this
    never blocks users). Cached ``_FREE_TIER_CACHE_TTL`` seconds so an upgrade shows within minutes.
    ``cached_only`` returns the live cached answer or the fail-open ``False`` without contacting Portal."""
    now = time.monotonic()
    profile_key = _pricing_profile_key()
    if not force_fresh:
        cached_result = get_cached_nous_free_tier()
        if cached_result is not None:
            return cached_result
    if cached_only:
        return False
    try:
        from hermes_cli.nous_account import get_nous_portal_account_info

        result = get_nous_portal_account_info(force_fresh=force_fresh).is_free_tier
    except Exception:
        result = False  # default to paid on error — don't block users
    _free_tier_cache[profile_key] = (result, now)
    return result


# ---------------------------------------------------------------------------
# Nous Portal recommended models — curated paid/free suggestions plus dedicated compaction (aux)
# and vision picks, TTL-cached per process. Fields read: {paid,free}RecommendedModels:
# [{modelName}], {paid,free}Recommended{Compaction,Vision}Model: {modelName} | null
# ---------------------------------------------------------------------------

NOUS_RECOMMENDED_MODELS_PATH = "/api/nous/recommended-models"
_NOUS_RECOMMENDED_CACHE_TTL: int = 600  # seconds (10 minutes)
# (result_dict, monotonic timestamp), scoped to the profile and portal.
_nous_recommended_cache: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}


def _nous_recommended_disk_path() -> "Path":
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "cache" / "nous_recommended_cache.json"


def _read_nous_recommended_disk(base: str) -> tuple[dict[str, Any], float] | None:
    """Return the last good payload and its age for the freshness check."""
    blob = _read_json_cache(_nous_recommended_disk_path(), errors=(OSError, json.JSONDecodeError, UnicodeDecodeError))
    entry = (blob or {}).get(base)
    data = entry.get("data") if isinstance(entry, dict) else None
    if not isinstance(data, dict) or not data:
        return None
    try:
        age = time.time() - float(entry.get("ts", 0))
    except (TypeError, ValueError, OverflowError):
        age = float("inf")
    return data, age


def _write_nous_recommended_disk(base: str, data: dict[str, Any]) -> None:
    """Merge ``data`` into the per-base disk map atomically; failures are debug-logged (the in-process
    cache still works)."""
    if not data:
        return
    path = _nous_recommended_disk_path()
    try:
        blob = _read_json_cache(path, errors=(OSError, json.JSONDecodeError, UnicodeDecodeError)) or {}
        blob[base] = {"data": data, "ts": time.time()}
        _write_json_cache(path, blob, indent=2)
    except OSError as exc:
        logger.debug("nous recommended-models disk cache write failed: %s", exc)


def fetch_nous_recommended_models(
    portal_base_url: str = "", timeout: float = 5.0, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Fetch the Portal's public ``/api/nous/recommended-models`` payload (no auth).

    Reuse successful results for ``_NOUS_RECOMMENDED_CACHE_TTL`` seconds, including across
    process restarts. ``force_refresh`` bypasses both caches. Stale disk data remains a fallback
    on live failure; reading it never renews its freshness.
    """
    base = (portal_base_url or "https://portal.nousresearch.com").rstrip("/")
    now = time.monotonic()
    cache_key = (_pricing_profile_key(), base)
    cached = _nous_recommended_cache.get(cache_key)
    if not force_refresh and cached is not None and now - cached[1] < _NOUS_RECOMMENDED_CACHE_TTL:
        return cached[0]
    disk = _read_nous_recommended_disk(base)
    if not force_refresh and disk is not None and 0 <= disk[1] < _NOUS_RECOMMENDED_CACHE_TTL:
        data, age = disk
        _nous_recommended_cache[cache_key] = (data, now - age)
        return data
    try:
        data = _get_json(
            f"{base}{NOUS_RECOMMENDED_MODELS_PATH}", timeout=timeout,
            headers={"Accept": "application/json", "Accept-Encoding": "gzip"}
        )
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    if data:
        _write_nous_recommended_disk(base, data)
    else:
        data = disk[0] if disk is not None else data
    _nous_recommended_cache[cache_key] = (data, now)
    return data


def _resolve_nous_portal_url() -> str:
    """Best-effort lookup of the Portal base URL the user is authed against."""
    try:
        from hermes_cli.auth import DEFAULT_NOUS_PORTAL_URL, get_provider_auth_state

        state = get_provider_auth_state("nous") or {}
        portal = str(state.get("portal_base_url") or "").strip()
        return (portal or str(DEFAULT_NOUS_PORTAL_URL)).rstrip("/")
    except Exception:
        return "https://portal.nousresearch.com"


def _extract_model_name(entry: Any) -> Optional[str]:
    """Pull the ``modelName`` field from a recommended-model entry, else None."""
    model_name = entry.get("modelName") if isinstance(entry, dict) else None
    return model_name.strip() if isinstance(model_name, str) and model_name.strip() else None


def get_nous_recommended_aux_model(
    *, vision: bool = False, free_tier: Optional[bool] = None, portal_base_url: str = "",
    force_refresh: bool = False) -> Optional[str]:
    """The Portal's recommended model for an auxiliary task: free tier → free pick only; paid tier →
    paid pick, falling back to the free one when the Portal returned ``null`` (staged rollouts)."""
    base = portal_base_url or _resolve_nous_portal_url()
    payload = fetch_nous_recommended_models(base, force_refresh=force_refresh)
    if not payload:
        return None
    if free_tier is None:
        try:
            free_tier = check_nous_free_tier()
        except Exception:
            free_tier = False  # assume paid on detection error — paid users see both fields anyway
    kind = "Vision" if vision else "Compaction"
    tiers = ("free",) if free_tier else ("paid", "free")
    return next((n for t in tiers if (n := _extract_model_name(payload.get(f"{t}Recommended{kind}Model")))), None)


def get_preferred_silent_default_model(provider: str = "openrouter") -> str:
    """Silent-default model id: the cached remote catalog's ``"default": true`` label (never hits the
    network — safe on hot paths), else :data:`PREFERRED_SILENT_DEFAULT_MODEL`."""
    try:
        from hermes_cli.model_catalog import get_default_model_from_cache
        labeled = get_default_model_from_cache(provider)
        if labeled:
            return labeled
    except Exception:
        pass
    return PREFERRED_SILENT_DEFAULT_MODEL


def pick_silent_default_model(model_ids: list[str], provider: str = "openrouter") -> str:
    """Catalog-labeled default when ``model_ids`` carries it, else the first entry, else "". Used by
    every surface that must choose a model without an interactive picker."""
    preferred = get_preferred_silent_default_model(provider)
    return preferred if preferred in model_ids else (model_ids[0] if model_ids else "")


def recommended_nous_default_model() -> dict[str, Any]:
    """The model a Nous account lands on without choosing one, honouring the account's tier.

    Curated catalog plus the Portal's recommendations for the tier, narrowed to the org's policy,
    then (free tier) to the rows the tier may select, then :func:`pick_silent_default_model`.
    Contacts the Portal for a fresh tier read, so never call it on a hot path. Returns
    ``{"provider": "nous", "model": str, "free_tier": bool}``; ``model`` may be ``""`` when nothing
    is selectable (callers degrade). Shared by ``GET /api/model/recommended-default`` and the
    sign-in completion in ``hermes_cli.anon_auth`` so both land on the same model.
    """
    from hermes_cli import models_pricing as mp
    from hermes_cli.auth import get_provider_auth_state

    model_ids = get_curated_nous_model_ids()
    pricing = mp.get_pricing_for_provider("nous") or {}
    free_tier = check_nous_free_tier(force_fresh=True)
    try:
        portal_url = (get_provider_auth_state("nous") or {}).get("portal_base_url", "") or ""
    except Exception:
        portal_url = ""
    # Narrow to policy BEFORE the tier split, so a rescued id still has to pass the free/paid predicate.
    policy_allowed = mp.nous_policy_allowed_ids()
    union = union_with_portal_free_recommendations if free_tier else union_with_portal_paid_recommendations
    model_ids, pricing = union(model_ids, pricing, portal_url)
    model_ids = mp.restrict_to_nous_policy(model_ids, policy_allowed, rescue_empty=True)
    if free_tier:
        model_ids, _unavailable = partition_nous_models_by_tier(model_ids, pricing, free_tier=True)
        # Never default onto a subscription-billed row: spending that plan is the user's call.
        model_ids = [mid for mid in model_ids if not _is_subscription_billed(pricing.get(mid))] or model_ids
    return {"provider": "nous", "model": pick_silent_default_model(model_ids, provider="nous"),
            "free_tier": bool(free_tier)}


def get_default_model_for_provider(provider: str) -> str:
    """Cost-safe default model for a provider, or "" — the NON-INTERACTIVE fallback when a provider
    is configured but no model was ever selected."""
    models = _PROVIDER_MODELS.get(provider, [])
    if provider in _SILENT_DEFAULT_PROVIDERS:
        preferred = get_preferred_silent_default_model(provider)
        # Trust the preferred default even without a static catalog (OpenRouter's picker list is
        # fetched live; its curated snapshot carries the default).
        if preferred and (preferred in models or not models):
            return preferred
    return models[0] if models else ""


def _openrouter_model_is_free(pricing: Any) -> bool:
    return _zero_priced(pricing, ("prompt", "completion"), "0")


def _openrouter_model_supports_tools(item: Any) -> bool:
    """True when ``supported_parameters`` advertises ``tools`` (hermes-agent is tool-calling-first).
    Permissive when the field is absent/malformed: some OpenRouter-compatible gateways (Nous Portal,
    private mirrors) don't populate it, and the picker must not silently empty for them.

    Ported from Kilo-Org/kilocode#9068.
    """
    params = item.get("supported_parameters") if isinstance(item, dict) else None
    return "tools" in params if isinstance(params, list) else True


# Reasoning-capability cache slots, one set per catalog (OpenRouter, Nous Portal). The logic
# lives in models_reasoning_caps and reads/writes these by name so tests can reset them here.
# ``*_cache``: model id → parsed caps for the process lifetime; ``*_failed_at``: monotonic time
# of the last failed fetch (60s re-fetch suppression); the flags are once-per-process guards.
_openrouter_reasoning_caps_cache: dict[str, Optional[dict[str, Any]]] | None = None
_openrouter_reasoning_caps_failed_at: float | None = None
_openrouter_caps_disk_checked = False
_openrouter_caps_warm_started = False
_nous_reasoning_caps_cache: dict[str, Optional[dict[str, Any]]] | None = None
_nous_reasoning_caps_failed_at: float | None = None
_nous_caps_disk_checked = False
_nous_caps_warm_started = False


from agent.reasoning_effort import CODEX_ASTRA_EFFORTS, clamp_effort as _clamp_effort, is_astra_model


def clamp_reasoning_effort_to_supported(
    effort: Optional[str], supported_efforts: Optional[list[str]]) -> Optional[str]:
    """Thin wrapper over :func:`agent.reasoning_effort.clamp_effort`: keep a supported level verbatim,
    else the nearest WEAKER supported level (never silently escalate cost), else the weakest; unknown
    supported-sets and bespoke level names pass through unchanged."""
    return _clamp_effort(effort, supported_efforts)


def clamp_github_reasoning_effort(effort: Any, supported: list[str]) -> str:
    """Copilot/GitHub Models effort for a non-empty *supported* list: the level itself when listed,
    else the nearest WEAKER listed level; bespoke names the ladder can't place fall to ``medium``
    (or the first listed level)."""
    effort = str(effort or "medium").strip().lower()
    if effort not in supported:
        effort = _clamp_effort(effort, supported)
        if effort not in supported:
            effort = "medium" if "medium" in supported else supported[0]
    return effort


def _fetch_live_catalog_index(url: str, timeout: float, opener) -> Optional[tuple[list, dict[str, dict[str, Any]]]]:
    """GET an OpenAI-style ``/models`` listing → ``(raw data array, {id: item})``, or None when the
    endpoint is unreachable or the payload has no ``data`` list."""
    try:
        payload = _get_json(url, timeout=timeout, headers={"Accept": "application/json"}, opener=opener)
    except Exception:
        return None
    live_items = payload.get("data", [])
    if not isinstance(live_items, list):
        return None
    live_by_id = {
        mid: item for item in live_items if isinstance(item, dict) and (mid := str(item.get("id") or "").strip())
    }
    return live_items, live_by_id


def fetch_openrouter_models(
    timeout: float = 8.0,
    *,
    force_refresh: bool = False,
    return_catalog: bool = False,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
) -> list[tuple[str, str]] | ProviderModelCatalog:
    """Return the curated OpenRouter picker list, refreshed from the live catalog when possible."""
    global _openrouter_catalog_cache

    route_scoped = any(value is not None for value in (api_key, base_url, headers))
    if (
        _openrouter_catalog_cache is not None
        and not force_refresh
        and not route_scoped
    ):
        cached = list(_openrouter_catalog_cache)
        return (
            _catalog_result(cached, verified_models=[mid for mid, _ in cached])
            if return_catalog else cached
        )

    # Prefer the remotely-hosted catalog manifest; fall back to the in-repo
    # snapshot when the manifest is unreachable. Both are curated lists that
    # drive the picker; the OpenRouter live /v1/models filter (tool support,
    # free pricing) is applied on top either way.
    try:
        from hermes_cli.model_catalog import get_curated_openrouter_models
        remote = get_curated_openrouter_models()
    except Exception:
        remote = None
    fallback = list(remote) if remote else list(OPENROUTER_MODELS)
    preferred_ids = [mid for mid, _ in fallback]
    try:
        catalog_base = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
        request_headers = {"Accept": "application/json"}
        if api_key:
            request_headers["Authorization"] = f"Bearer {api_key}"
        if headers:
            request_headers.update(headers)
        req = urllib.request.Request(
            f"{catalog_base}/models",
            headers=request_headers,
        )
        with _urlopen_model_catalog_request(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except Exception:
        result = list(_openrouter_catalog_cache or fallback)
        return _catalog_result(result) if return_catalog else result

    live_items = payload.get("data", [])
    if not isinstance(live_items, list):
        result = list(_openrouter_catalog_cache or fallback)
        return _catalog_result(result) if return_catalog else result

    live_by_id: dict[str, dict[str, Any]] = {}
    for item in live_items:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        live_by_id[mid] = item

    # Free warm-up for the reasoning-capability cache: this is the same
    # payload _fetch_openrouter_reasoning_caps would fetch, so parse it once
    # here and hot-path callers (openrouter_model_reasoning_capabilities)
    # never need their own HTTP round-trip.
    global _openrouter_reasoning_caps_cache
    if _openrouter_reasoning_caps_cache is None and live_by_id:
        _openrouter_reasoning_caps_cache = {
            mid: parse_openrouter_reasoning_capabilities(item)
            for mid, item in live_by_id.items()
        }

    curated: list[tuple[str, str]] = []
    silent_default = get_preferred_silent_default_model("openrouter")
    for preferred_id in preferred_ids:
        live_item = live_by_id.get(preferred_id)
        if live_item is None:
            continue
        # Hide models that don't advertise tool-calling support — hermes-agent
        # requires it and surfacing them leads to immediate runtime failures
        # when the user selects them. Ported from Kilo-Org/kilocode#9068.
        if not _openrouter_model_supports_tools(live_item):
            continue
        if preferred_id == silent_default:
            # Keep the silent-default badge through the live refresh so the
            # picker shows which model Hermes lands on when none is selected.
            desc = "default"
        else:
            desc = "free" if _openrouter_model_is_free(live_item.get("pricing")) else ""
        curated.append((preferred_id, desc))

    if not curated:
        result = list(_openrouter_catalog_cache or fallback)
        return _catalog_result(result) if return_catalog else result

    first_id, first_desc = curated[0]
    if not first_desc:
        curated[0] = (first_id, "recommended")
    if not route_scoped:
        _openrouter_catalog_cache = curated
    if return_catalog:
        ids = [mid for mid, _ in curated]
        return _catalog_result(ids, verified_models=ids)
    return list(curated)


def model_ids(
    *,
    force_refresh: bool = False,
    return_catalog: bool = False,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
) -> list[str]:
    """Return just the OpenRouter model-id strings."""
    fetch_kwargs = {
        "force_refresh": force_refresh,
        "return_catalog": return_catalog,
    }
    if any(value is not None for value in (api_key, base_url, headers)):
        fetch_kwargs.update(
            {"api_key": api_key, "base_url": base_url, "headers": headers}
        )
    catalog = fetch_openrouter_models(**fetch_kwargs)
    if return_catalog and isinstance(catalog, ProviderModelCatalog):
        return _catalog_result(catalog, verified_models=catalog.verified_models)
    return [mid for mid, _ in catalog]


def get_curated_nous_model_ids() -> list[str]:
    """Curated Nous Portal model ids: the remote catalog manifest, else the in-repo
    ``_PROVIDER_MODELS["nous"]`` snapshot. Always a list."""
    try:
        from hermes_cli.model_catalog import get_curated_nous_models
        remote = get_curated_nous_models()
    except Exception:
        remote = None
    return list(remote or _PROVIDER_MODELS.get("nous", []))


def _ai_gateway_model_is_free(pricing: Any) -> bool:
    return _zero_priced(pricing, ("input", "output"), "0")


def fetch_ai_gateway_models(
    timeout: float = 8.0, *, force_refresh: bool = False) -> list[tuple[str, str]]:
    """Return the curated AI Gateway picker list, refreshed from the live catalog when possible."""
    global _ai_gateway_catalog_cache

    if _ai_gateway_catalog_cache is not None and not force_refresh:
        return list(_ai_gateway_catalog_cache)

    from hermes_constants import AI_GATEWAY_BASE_URL

    fallback = list(VERCEL_AI_GATEWAY_MODELS)
    live = _fetch_live_catalog_index(f"{AI_GATEWAY_BASE_URL.rstrip('/')}/models", timeout, _urlopen_model_catalog_request)
    if live is None:
        return list(_ai_gateway_catalog_cache or fallback)
    _, live_by_id = live

    curated = [
        (pid, "free" if _ai_gateway_model_is_free(live_by_id[pid].get("pricing")) else "")
        for pid, _ in fallback if pid in live_by_id]
    if not curated:
        return list(_ai_gateway_catalog_cache or fallback)

    # A free Moonshot model in the live catalog is auto-promoted to #1 as "recommended".
    free_moonshot = next(
        (mid for mid, item in live_by_id.items()
         if mid.startswith("moonshotai/") and _ai_gateway_model_is_free(item.get("pricing"))),
        None)
    if free_moonshot:
        curated = [(free_moonshot, "recommended")] + [(mid, desc) for mid, desc in curated if mid != free_moonshot]
    else:
        curated[0] = (curated[0][0], "recommended")
    _ai_gateway_catalog_cache = curated
    return list(curated)


def ai_gateway_model_ids(*, force_refresh: bool = False) -> list[str]:
    """Return just the AI Gateway model-id strings."""
    return [mid for mid, _ in fetch_ai_gateway_models(force_refresh=force_refresh)]


# ---------------------------------------------------------------------------
# Provider identity: ``provider:model`` parsing, auto-detection, labels
# ---------------------------------------------------------------------------

# All provider IDs and aliases valid on the left of the ``provider:model`` syntax.
_KNOWN_PROVIDER_NAMES: set[str] = set(_PROVIDER_LABELS) | set(_PROVIDER_ALIASES) | {"openrouter", "custom"}


_CONFIG_ERRORS = (ImportError, OSError, RuntimeError, TypeError, ValueError, AttributeError)


def _configured_custom_provider_ids() -> set[str]:
    """Return routable custom-provider IDs configured by the user."""
    ids = {"custom"}
    try:
        from hermes_cli.config import load_config
        from hermes_cli.providers import custom_provider_slug

        config = load_config()
        providers = config.get("providers", {})
        if isinstance(providers, dict):
            ids.update(custom_provider_slug(str(entry.get("name") or key), str(key))
                       for key, entry in providers.items() if isinstance(entry, dict))
        legacy = config.get("custom_providers", [])
        if isinstance(legacy, list):
            ids.update(
                custom_provider_slug(str(entry.get("name") or "")) for entry in legacy if isinstance(entry, dict))
    except _CONFIG_ERRORS:
        pass
    return ids


def _provider_has_credentials(pid: str) -> bool:
    try:
        from hermes_cli.auth import get_auth_status, has_usable_secret

        if pid == "custom":
            return bool((_get_custom_base_url() or "").strip())
        if pid == "openrouter":
            from hermes_cli.model_switch import _scoped_key_env
            return has_usable_secret(_scoped_key_env("OPENROUTER_API_KEY"))
        status = get_auth_status(pid)
        return bool(status.get("logged_in") or status.get("configured"))
    except Exception:
        return False


def list_available_providers() -> list[dict[str, str]]:
    """``{id, label, aliases, authenticated}`` for every provider usable with ``provider:model``,
    derived from :data:`CANONICAL_PROVIDERS` (shared with ``hermes model`` and ``/model``)."""
    aliases_for: dict[str, list[str]] = {}
    for alias, canonical in _PROVIDER_ALIASES.items():
        aliases_for.setdefault(canonical, []).append(alias)
    return [
        {
            "id": pid,
            "label": _PROVIDER_LABELS.get(pid, pid),
            "aliases": aliases_for.get(pid, []),
            "authenticated": _provider_has_credentials(pid)}
        for pid in [p.slug for p in CANONICAL_PROVIDERS] + ["custom"]]


def parse_model_input(
        raw: str, current_provider: str, *, custom_ids: Optional[set[str]] = None) -> tuple[str, str]:
    """Parse ``/model`` input into ``(provider, model)``. The colon is a provider delimiter only when
    the left side is a known provider/alias, so ``anthropic/claude-3.5-sonnet:beta`` stays a model.
    ``custom_ids`` is the caller's already-loaded set of configured ``custom:<name>`` ids (default:
    read from config) so one decision never consults two config sources."""
    stripped = raw.strip()
    colon = stripped.find(":")
    if colon > 0:
        provider_part = stripped[:colon].strip().lower()
        model_part = stripped[colon + 1:].strip()
        if provider_part and model_part and provider_part in _KNOWN_PROVIDER_NAMES:
            if provider_part == "custom":
                configured = _configured_custom_provider_ids() if custom_ids is None else custom_ids
                # Longest configured ``custom:<name>`` id that prefixes the input wins.
                lowered = stripped.lower()
                for custom_id in sorted(configured - {"custom"}, key=len, reverse=True):
                    if lowered.startswith(f"{custom_id.lower()}:"):
                        return custom_id, stripped[len(custom_id) + 1 :].strip()
                # ``custom:local:qwen`` → ("custom:local", "qwen") for a configured named provider;
                # single-colon ``custom:qwen`` → ("custom", "qwen") as before.
                if ":" in model_part:
                    custom_name, actual_model = (part.strip() for part in model_part.split(":", 1))
                    if custom_name and actual_model:
                        if f"custom:{custom_name.lower()}" in configured:
                            return (f"custom:{custom_name.lower()}", actual_model)
                        return ("custom", model_part)
            return (normalize_provider(provider_part), model_part)
    return (current_provider, stripped)


def _get_custom_base_url() -> str:
    """The custom endpoint ``model.base_url`` from config.yaml."""
    return str(_get_model_config_dict().get("base_url", "")).strip()


def _get_provider_config_dict(provider: str) -> dict[str, Any]:
    """Return config.yaml providers.<provider>, or an empty dict."""
    key = str(provider or "").strip()
    if not key:
        return {}
    try:
        from hermes_cli.config import load_config
        providers_cfg = load_config().get("providers", {})
        if isinstance(providers_cfg, dict):
            entry = providers_cfg.get(key) or providers_cfg.get(key.lower())
            if isinstance(entry, dict):
                return entry
    except _CONFIG_ERRORS:
        pass
    return {}


def _get_model_config_dict() -> dict[str, Any]:
    """Return the main model config mapping, or an empty dict."""
    try:
        from hermes_cli.config import load_config
        model_cfg = load_config().get("model", {})
        if isinstance(model_cfg, dict):
            return model_cfg
    except Exception:
        pass
    return {}


def _base_url_looks_like_anthropic_messages(base_url: str) -> bool:
    normalized = str(base_url or "").strip().lower().rstrip("/")
    if not normalized:
        return False
    return urllib.parse.urlparse(normalized).path.rstrip("/").endswith(("/anthropic", "/anthropic/v1"))


def _anthropic_models_url(base_url: Optional[str] = None, *, after_id: Optional[str] = None) -> str:
    """Anthropic ``/v1/models`` page URL. The endpoint is cursor-paginated with a default page of
    20 (smaller than the live catalog), so every request asks for the maximum page size and
    ``after_id`` continues from a previous page's ``last_id``."""
    endpoint = str(base_url or "https://api.anthropic.com").strip().rstrip("/")
    url = endpoint + ("/models" if endpoint.endswith("/v1") else "/v1/models")
    params = {"limit": "1000"}
    if after_id:
        params["after_id"] = after_id
    return url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)


_ANTHROPIC_MODELS_MAX_PAGES = 20


def _anthropic_next_cursor(page: Any, seen_cursors: set[str]) -> Optional[str]:
    """``last_id`` to continue from, or None when the page is final or the server repeats a
    cursor (which would otherwise loop forever)."""
    if not isinstance(page, dict) or page.get("has_more") is not True:
        return None
    last_id = page.get("last_id")
    if not isinstance(last_id, str) or not last_id or last_id in seen_cursors:
        return None
    seen_cursors.add(last_id)
    return last_id


def curated_models_for_provider(
    provider: Optional[str],
    *,
    force_refresh: bool = False,
) -> list[tuple[str, str]]:
    """Return ``(model_id, description)`` tuples for a provider's model list.

    Tries to fetch the live model list from the provider's API first,
    falling back to the static ``_PROVIDER_MODELS`` catalog if the API
    is unreachable.
    """
    normalized = normalize_provider(provider)
    if normalized == "openrouter":
        return fetch_openrouter_models(force_refresh=force_refresh)

    # Try live API first (Codex, Nous, etc. all support /models)
    live = provider_model_ids(normalized)
    if live:
        return [(m, "") for m in live]

    # Fallback to static catalog
    models = _PROVIDER_MODELS.get(normalized, [])
    return [(m, "") for m in models]


def _provider_keys(provider: str) -> set[str]:
    key = (provider or "").strip().lower()
    normalized = normalize_provider(provider)
    return {k for k in (key, normalized) if k}


def _provider_catalog_names(provider: str) -> tuple[str, ...]:
    """Active picker models plus retired aliases recognized for detection."""
    return tuple(_PROVIDER_MODELS.get(provider, [])) + _PROVIDER_RETIRED_ALIASES.get(provider, ())


def _model_in_provider_catalog(name_lower: str, providers: set[str]) -> bool:
    return any(
        name_lower == model.lower()
        for provider in providers
        for model in _provider_catalog_names(provider))


def _resolve_static_model_alias(
    name_lower: str, current_keys: set[str]) -> Optional[tuple[str, str]]:
    """Resolve short aliases (e.g. sonnet/opus) using static catalogs only."""
    try:
        from hermes_cli.model_switch import MODEL_ALIASES
    except Exception:
        return None

    identity = MODEL_ALIASES.get(name_lower)
    if identity is None:
        return None

    def _match(provider: str) -> Optional[str]:
        prefix = f"{identity.vendor}/{identity.family}" if provider in _AGGREGATOR_PROVIDERS else identity.family
        prefix = prefix.lower()
        return next((m for m in _PROVIDER_MODELS.get(provider, []) if m.lower().startswith(prefix)), None)

    # Current provider first, then native vendors, then aggregators / borrow-list providers the user
    # is already on — so `sonnet` resolves to anthropic before any re-exposing provider.
    skip = current_keys | _AGGREGATOR_PROVIDERS | _BORROWED_MODEL_PROVIDERS
    candidates = [
        *current_keys, *(p for p in _PROVIDER_MODELS if p not in skip),
        *(p for p in _AGGREGATOR_PROVIDERS if p in current_keys),
        *(p for p in _BORROWED_MODEL_PROVIDERS if p in current_keys)]
    for provider in candidates:
        if matched := _match(provider):
            return provider, matched
    return None


def detect_static_provider_for_model(
    model_name: str, current_provider: str) -> Optional[tuple[str, str]]:
    """Auto-detect a provider from static catalogs only → ``(provider_id, model_name)`` (the name may
    be remapped by a static alias or a bare provider name), or ``None`` without a confident match."""
    name = (model_name or "").strip()
    if not name:
        return None

    name_lower = name.lower()
    current_keys = _provider_keys(current_provider)

    alias_match = _resolve_static_model_alias(name_lower, current_keys)
    if alias_match:
        return alias_match

    # Step 0: a bare provider name typed as the model (`/model nous`) is a provider switch to that
    # provider's default. Skip "custom" (no catalog) and "openrouter" (needs an explicit model).
    resolved_provider = _PROVIDER_ALIASES.get(name_lower, name_lower)
    if resolved_provider not in {"custom", "openrouter"}:
        default_models = _PROVIDER_MODELS.get(resolved_provider, [])
        if resolved_provider in _PROVIDER_LABELS and default_models and resolved_provider not in current_keys:
            # Cost-safe default, not ``default_models[0]``: metered aggregators list most-capable-first,
            # so [0] would silently escalate `/model nous` to the priciest flagship.
            return (resolved_provider, get_default_model_for_provider(resolved_provider) or default_models[0])

    # A model in the current provider's own catalog never suggests switching.
    if _model_in_provider_catalog(name_lower, current_keys):
        return None

    return next(_static_catalog_matches(name, current_provider), None)


def _static_catalog_matches(name: str, current_provider: str):
    """Yield every ``(provider_id, name)`` whose static catalog lists *name*, in ladder order.

    Several first-party providers list the same slug (``gpt-5.6-luna`` on ``openai-api`` AND
    ``openai-codex``); the first is only a guess, so callers that gate on credentials need the
    siblings too (#102775)."""
    name_lower = name.lower()
    current_keys = _provider_keys(current_provider)
    # Step 1: direct static-catalog match. Aggregators list other vendors' models — never
    # auto-switch TO them. A custom endpoint (custom / custom:*) is never auto-switched away
    # from: the user configured it deliberately and may serve the same model name there.
    if current_provider != "custom" and not current_provider.startswith("custom:"):
        for pid in _PROVIDER_MODELS:
            if pid in current_keys or pid in _AGGREGATOR_PROVIDERS or pid in _BORROWED_MODEL_PROVIDERS:
                continue
            if _model_in_provider_catalog(name_lower, {pid}):
                yield (pid, name)

    # Borrow-list providers (re-expose other vendors' models) only after every native-vendor
    # catalog, and only when one is the current provider.
    for pid in _BORROWED_MODEL_PROVIDERS:
        if pid not in current_keys and _model_in_provider_catalog(name_lower, {pid}):
            yield (pid, name)


def _configured_provider_ids() -> set[str]:
    """Provider ids (incl. ``custom:*``) from the user's ``providers:`` config block; empty when config
    is unreadable (callers fall through to built-in catalogs)."""
    try:
        from hermes_cli.config import load_config

        providers = (load_config() or {}).get("providers")
        if not isinstance(providers, dict):
            return set()
        return {key for pid in providers if (key := str(pid).strip().lower())}
    except Exception:
        return set()


def _resolve_provider_prefix(model_name: str) -> Optional[tuple[str, str]]:
    """Route an explicit ``vendor/model`` prefix (``nous/deepseek-v4-pro``, ``ollama/qwen3.5:4b``) to
    a provider the user defined in ``providers:`` (by raw name or alias) instead of the default.

    ``nous/deepseek-v4-pro`` or ``ollama/qwen3.5:4b`` should route to the named provider instead of falling
    back to the configured default (which silently sends non-default models to the wrong endpoint, #87189).
    """
    if "/" not in model_name:
        return None
    vendor, model = model_name.split("/", 1)
    vendor, model = vendor.strip().lower(), model.strip()
    if not vendor or not model:
        return None
    configured = _configured_provider_ids()
    # An explicitly named provider block (``ollama:``) wins over the alias table, which may
    # canonicalize the same name elsewhere (``ollama`` → ``custom``).
    for candidate in (vendor, _PROVIDER_ALIASES.get(vendor, vendor)):
        if candidate in configured:
            return (candidate, model)
    return None


def detect_provider_for_model(
    model_name: str, current_provider: str) -> Optional[tuple[str, str]]:
    """Auto-detect the best provider for a model name: the current provider's live catalog, static
    catalogs (bare provider name → its default; direct catalog match), then the OpenRouter catalog,
    then a configured ``vendor/`` prefix.

    Never hands back a provider the user holds no credentials for: an unauthenticated guess is
    skipped and the ladder continues (``None`` = stay on the current provider). Exceptions: the user
    NAMED the provider (``/model nous``), or there is no current provider yet (``auto``) — then the
    first guess is returned so the credential step fails loudly instead of silently ignoring input."""
    from hermes_cli.models_detect import (
        current_provider_catalog_match, current_provider_owns_vendor, provider_has_credentials)

    name = (model_name or "").strip()
    if not name:
        return None

    # The current provider's LIVE catalog outranks every static guess: a model it already serves
    # (Codex early-access ids, Portal-only slugs, Ollama Cloud models absent from _PROVIDER_MODELS)
    # must never re-route the session to another vendor or to metered OpenRouter.
    served = current_provider_catalog_match(name, current_provider)
    if served is not None:
        return (current_provider, served) if served != name else None
    # Live catalog unavailable or lagging: the vendor's own id on the vendor's first-party provider
    # is still a selection — an aggregator relisting it is not grounds to switch.
    if current_provider_owns_vendor(name, current_provider):
        return None

    no_selection = (current_provider or "").strip().lower() in {"", "auto"}
    first_guess = None
    for candidate in _detection_candidates(name, current_provider):
        if candidate is None:
            return None  # the current catalog owns this name
        if candidate[0] == current_provider or provider_has_credentials(candidate[0]):
            return candidate
        if _PROVIDER_ALIASES.get(name.lower(), name.lower()) == candidate[0]:
            return candidate  # explicitly named provider: let the credential step report it
        first_guess = first_guess or candidate
        logger.debug("Skipping auto-switch of '%s' to %s: no credentials configured", name, candidate[0])
    if no_selection and first_guess:
        return first_guess  # nothing usable anywhere: fail loudly on the first guess
    # A ``vendor/model`` prefix naming a provider the user DECLARED in ``providers:`` is a selection,
    # not a guess — hand it back even before its key is wired up.
    return _resolve_provider_prefix(name)


def _detection_candidates(name: str, current_provider: str):
    """Yield ``(provider, model)`` guesses in ladder order; ``None`` means the current provider's own
    catalog owns the name (stop, stay)."""
    static_match = detect_static_provider_for_model(name, current_provider)
    if static_match:
        yield static_match
        # Sibling catalogs listing the same slug (openai-api / openai-codex share the gpt-5.6
        # family): the credential gate downstream takes the first one the user can actually use.
        for sibling in _static_catalog_matches(name, current_provider):
            if sibling != static_match:
                yield sibling
    if _model_in_provider_catalog(name.lower(), _provider_keys(current_provider)):
        yield None
        return

    # OpenRouter catalog (exact slug, then bare model part).
    or_slug = _find_openrouter_slug(name)
    if or_slug:
        if current_provider == "openrouter" and or_slug == name:
            yield None  # already on openrouter with matching name
            return
        yield ("openrouter", or_slug)

    # Explicit ``vendor/model`` prefix naming a configured provider — AFTER the OpenRouter lookup so
    # aggregator-native slugs (``deepseek/deepseek-chat``) keep their routing.
    prefixed = _resolve_provider_prefix(name)
    if prefixed:
        yield prefixed


def _find_openrouter_slug(model_name: str) -> Optional[str]:
    """Full OpenRouter slug for a bare or partial model name (exact slug first, then bare part)."""
    name_lower = model_name.strip().lower()
    if not name_lower:
        return None
    ids = model_ids()
    return (
        next((mid for mid in ids if name_lower == mid.lower()), None)
        or next((mid for mid in ids if "/" in mid and name_lower == mid.split("/", 1)[1].lower()), None)
    )


def normalize_provider(provider: Optional[str]) -> str:
    """Normalize provider aliases to canonical ids. ``"auto"`` passes through — use
    ``hermes_cli.auth.resolve_provider()`` to resolve it from credentials."""
    normalized = (provider or "openrouter").strip().lower()
    return _PROVIDER_ALIASES.get(normalized, normalized)


def provider_label(provider: Optional[str]) -> str:
    """Return a human-friendly label for a provider id or alias."""
    original = (provider or "openrouter").strip()
    normalized = original.lower()
    if normalized == "auto":
        return "Auto"
    normalized = normalize_provider(normalized)
    return _PROVIDER_LABELS.get(normalized, original or "OpenRouter")


def _is_openai_fast_model(model_id: Optional[str]) -> bool:
    """OpenAI flagship eligible for Priority Processing. Codex-series excluded — the Codex Responses
    API doesn't accept ``service_tier``."""
    base = _strip_vendor_prefix(str(model_id or "")).split(":")[0]
    return bool(base) and "codex" not in base and base.startswith(tuple(_OPENAI_FAST_MODE_PREFIXES))


def _strip_vendor_prefix(model_id: str) -> str:
    """Lowercase and strip a ``vendor/`` prefix (``anthropic/claude-opus-4-6`` → ``claude-opus-4-6``)."""
    raw = str(model_id or "").strip().lower()
    return raw.split("/", 1)[1] if "/" in raw else raw


def model_supports_fast_mode(model_id: Optional[str]) -> bool:
    """Return whether Hermes should expose the /fast toggle for this model."""
    from agent.model_metadata import is_grok_46_family

    return (
        _is_anthropic_fast_model(model_id)
        or _is_openai_fast_model(model_id)
        or is_grok_46_family(str(model_id or "")))


def _is_anthropic_fast_model(model_id: Optional[str]) -> bool:
    """Accepts the Anthropic Fast Mode ``speed`` param (Opus 4.8 / Opus 5 / Opus 5.5 only) —
    deliberately NOT a general "fast model" check. The list lives in ``agent.model_metadata``."""
    from agent.model_metadata import is_anthropic_fast_mode_model

    return is_anthropic_fast_mode_model(model_id)


def _fast_mode_route_supported(
    model_id: Optional[str], provider: Optional[str], base_url: Optional[str]) -> bool:
    """Only the first-party endpoint that bills for fast mode may receive its params."""
    from urllib.parse import urlparse

    from agent.model_metadata import is_grok_46_family

    if _is_anthropic_fast_model(model_id):
        allowed = {"anthropic": "api.anthropic.com"}
    elif is_grok_46_family(str(model_id or "")):
        allowed = {"xai": "api.x.ai"}
    else:
        allowed = {"openai": "api.openai.com", "openai-codex": "chatgpt.com"}
    if provider and normalize_provider(provider) not in allowed:
        return False
    host = (urlparse(str(base_url or "")).hostname or "").lower()
    return not host or host in allowed.values()


def model_supports_ultrafast(model_id: Optional[str]) -> bool:
    """OpenAI Ultrafast (``service_tier: "ultrafast"``) is published per model, not per family."""
    from agent.model_metadata import strip_codex_context_variant_suffix

    base = _strip_vendor_prefix(strip_codex_context_variant_suffix(str(model_id or ""))).split(":")[0]
    return base in _OPENAI_ULTRAFAST_MODELS


def resolve_fast_mode_overrides(
    model_id: Optional[str], *, provider: Optional[str] = None, base_url: Optional[str] = None,
    tier: Optional[str] = None,
) -> dict[str, Any] | None:
    """Fast/priority request_overrides — ``{"speed": "fast"}`` (Anthropic Fast Mode) or
    ``{"service_tier": "priority"}`` (OpenAI / xAI Priority Processing) — or None if unsupported.
    ``tier="ultrafast"`` asks for OpenAI Ultrafast instead: ``{"service_tier": "ultrafast"}`` on an
    Ultrafast model, None elsewhere (never a silent downgrade to a different paid tier).
    With ``provider``/``base_url`` the route is gated too (``_fast_mode_route_supported``) so proxies
    never see the params. Single fast-mode gate for ``/fast`` and ``agent.fast_mode`` windows."""
    if not model_supports_fast_mode(model_id):
        return None
    if (provider or base_url) and not _fast_mode_route_supported(model_id, provider, base_url):
        return None
    if tier == "ultrafast":
        return {"service_tier": "ultrafast"} if model_supports_ultrafast(model_id) else None
    return {"speed": "fast"} if _is_anthropic_fast_model(model_id) else {"service_tier": "priority"}


def _first_exchangeable_copilot_token(raw_tokens) -> str:
    """Exchange stored GitHub tokens in order; the first that validates AND exchanges wins (every
    entry is tried so a later valid token survives an earlier malformed one)."""
    from hermes_cli.copilot_auth import exchange_copilot_token, validate_copilot_token

    for raw in raw_tokens:
        raw = str(raw or "").strip()
        if not raw or not validate_copilot_token(raw)[0]:
            continue
        try:
            api_token = exchange_copilot_token(raw)[0]  # (api_token, expires_at, base_url)
        except Exception:
            continue
        if api_token:
            return api_token
    return ""


def _copilot_cli_config_tokens() -> list[str]:
    """``copilotTokens`` from the GitHub Copilot CLI's own plaintext store (JSONC — strip
    ``//``-comment lines), written by ``copilot login`` on hosts without an OS keychain."""
    cli_config = os.path.expanduser("~/.copilot/config.json")
    if not os.path.isfile(cli_config):
        return []
    with open(cli_config, "r", encoding="utf-8-sig", errors="ignore") as fh:
        raw_text = "\n".join(
            line for line in fh.read().splitlines() if not line.lstrip().startswith("//"))
    data = json.loads(raw_text) if raw_text.strip() else {}
    tokens = data.get("copilotTokens")
    return list(tokens.values()) if isinstance(tokens, dict) else []


def _resolve_copilot_catalog_api_key() -> str:
    """Best-effort GitHub token for the Copilot catalog: env vars / ``gh auth token`` via
    ``resolve_api_key_provider_credentials``, then ``auth.json`` ``credential_pool.copilot[]``, then
    ``~/.copilot/config.json`` ``copilotTokens`` (the ACP CLI's own store). Without the latter two,
    keyless users see the picker fall back to the stale curated list on a silent 401."""
    def _pool_token() -> str:
        from hermes_cli.auth import read_credential_pool

        return _first_exchangeable_copilot_token(
            entry.get("access_token") for entry in read_credential_pool("copilot") if isinstance(entry, dict))

    sources = (
        lambda: _api_key_credentials("copilot")[0],
        _pool_token,
        lambda: _first_exchangeable_copilot_token(_copilot_cli_config_tokens()),
    )
    for source in sources:
        try:
            token = source()
        except Exception:
            continue
        if token:
            return token
    return ""


def _model_dedup_key(model_id: str) -> str:
    """Case-insensitive dedup key folded through the picker-search alias table, so a bare live wire
    id and its curated public slug (Kimi ``k3`` / ``kimi-k3``) don't both survive a merge."""
    key = str(model_id).strip().lower()
    try:
        from hermes_cli.model_search import model_alias_canonical
        return model_alias_canonical(key)
    except Exception:
        return key


def _merge_with_models_dev(provider: str, curated: list[str]) -> list[str]:
    """models.dev entries first (their order), then curated-only extras, case-insensitively deduped
    while preserving curated casing. Curated unchanged when models.dev is unreachable/empty."""
    try:
        from agent.models_dev import list_agentic_models
        mdev = list_agentic_models(provider)
    except Exception:
        mdev = []
    if not mdev:
        return list(curated)
    return _merge_unique(_merge_unique([], mdev), curated)


def _openai_discovery_base_url(provider: str) -> str:
    """OpenAI endpoint for model discovery, mirroring runtime precedence so discovery probes the SAME
    endpoint inference uses: ``$OPENAI_BASE_URL`` → config ``model.base_url`` (when the configured
    provider matches) → the canonical default."""
    env_raw = os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/")
    if env_raw:
        return env_raw
    try:
        model_cfg = _get_model_config_dict()
        cfg_provider = str(model_cfg.get("provider") or "").strip().lower()
        same_provider = normalize_provider(provider) == normalize_provider(cfg_provider)
        if cfg_provider in ("openai", "openai-api") and same_provider:
            cfg_url = str(model_cfg.get("base_url") or "").strip().rstrip("/")
            if cfg_url:
                return cfg_url
    except Exception:
        pass
    return "https://api.openai.com/v1"


def _codex_catalog(normalized: str, force_refresh: bool) -> list[str]:
    from hermes_cli.codex_models import get_codex_model_ids

    # Live OAuth token so the picker matches what ChatGPT lists for this account; hardcoded
    # catalog without a token / when unreachable. Read-only (#68004): a picker never imports,
    # refreshes or persists a credential, so an expired stored token means the hardcoded catalog
    # until the runtime lease refreshes it.
    # The token and the host it is routed to come from the same resolution (#121486): a pooled
    # gateway key is only ever sent to that gateway, never to the chatgpt.com default.
    base_url = None
    try:
        from hermes_cli.auth import _codex_access_token_is_expiring, resolve_codex_runtime_credentials

        creds = resolve_codex_runtime_credentials(read_only=True)
        access_token, base_url = creds.get("api_key"), creds.get("base_url")
        if _codex_access_token_is_expiring(access_token, 0):
            access_token = None
    except Exception:
        access_token = None
    return get_codex_model_ids(access_token=access_token, base_url=base_url)


_COPILOT_ACP_SESSION_MEMO_TTL = 300.0  # 5 min; SWR disk cache handles the rest
_COPILOT_ACP_SESSION_FAIL_TTL = 30.0  # failed probes re-probe quickly so a fresh CLI login is picked up
_copilot_acp_session_memo: Optional[tuple[float, float, Optional[list[str]]]] = None  # (at, ttl, models)


def _copilot_acp_session_models(force_refresh: bool) -> Optional[list[str]]:
    """Enabled models from a signed-in ``copilot --acp`` session, memoized for a few minutes —
    successes AND failures. Model-switch validation (``models_validate._static_catalog``) reads
    this uncached on every ``/model`` switch, and each miss is a CLI spawn + handshake (up to the
    probe timeout), so without the memo every switch paid a subprocess. A failed probe is
    memoized much more briefly so a user who signs in to the CLI right after a miss is picked up
    on the next switch (or immediately via ``/model --refresh``, which clears this memo)."""
    global _copilot_acp_session_memo
    now = time.monotonic()
    memo = _copilot_acp_session_memo
    if not force_refresh and memo is not None and now - memo[0] < memo[1]:
        return memo[2]
    from providers import get_provider_profile

    try:
        live = get_provider_profile("copilot-acp").fetch_models() or None
    except Exception:
        logger.debug("copilot-acp session model discovery failed", exc_info=True)
        live = None
    _copilot_acp_session_memo = (now, _COPILOT_ACP_SESSION_MEMO_TTL if live else _COPILOT_ACP_SESSION_FAIL_TTL, live)
    return live


class CuratedFallbackModels(list[str]):
    """A curated list served because the provider's live catalog was unavailable. The disk cache
    treats it as a placeholder, never as the account's real catalog (#107391)."""


def _copilot_catalog(normalized: str, force_refresh: bool) -> Optional[list[str]]:
    if normalized == "copilot-acp" and (live := _copilot_acp_session_models(force_refresh)):
        return live
    try:
        live = _fetch_github_models(_resolve_copilot_catalog_api_key())
        if live:
            return live
    except Exception:
        pass
    return CuratedFallbackModels(_PROVIDER_MODELS.get("copilot", []))


def _nous_catalog(normalized: str, force_refresh: bool) -> Optional[list[str]]:
    try:
        from hermes_cli.auth import fetch_nous_models, resolve_nous_runtime_credentials

        creds = resolve_nous_runtime_credentials()
        if creds:
            live = fetch_nous_models(api_key=creds.get("api_key", ""), inference_base_url=creds.get("base_url", ""))
            if live:
                return live
    except Exception:
        pass
    # Live failed / no creds: the docs-hosted manifest — NOT the in-repo snapshot — so newly added
    # Portal models still surface without a Hermes release.
    return get_curated_nous_model_ids() or None


def _api_key_credentials(normalized: str) -> tuple[str, str]:
    """``(api_key, base_url)`` from ``resolve_api_key_provider_credentials``; empty strings on any miss."""
    try:
        from hermes_cli.auth import resolve_api_key_provider_credentials

        creds = resolve_api_key_provider_credentials(normalized)
        return str(creds.get("api_key") or "").strip(), str(creds.get("base_url") or "").strip()
    except Exception:
        return "", ""


def _api_key_provider_live(normalized: str, force_refresh: bool) -> Optional[list[str]]:
    """Live /v1/models for a simple api-key provider (stepfun, gmi); None on any miss."""
    api_key, base_url = _api_key_credentials(normalized)
    if not (api_key and base_url):
        return None
    try:
        return fetch_api_models(api_key, base_url) or None
    except Exception:
        return None


def _anthropic_catalog(normalized: str, force_refresh: bool) -> list[str]:
    model_cfg = _get_model_config_dict()
    cfg_base_url = cfg_api_key = ""
    if normalize_provider(str(model_cfg.get("provider", "") or "")) == "anthropic":
        cfg_base_url = str(model_cfg.get("base_url", "") or "").strip()
        cfg_api_key = str(model_cfg.get("api_key", "") or "").strip()
    live = _fetch_anthropic_models(base_url=cfg_base_url or None, api_key=cfg_api_key or None)
    curated = list(_PROVIDER_MODELS.get("anthropic", []))
    if not live:
        return curated
    # The live /v1/models dump lags newly-routed curated aliases (reachable before enumerated):
    # curated first, then live-only extras, so a fresh curated model never disappears.
    return live if cfg_base_url else _merge_unique(curated, live)


def _openai_catalog(normalized: str, force_refresh: bool) -> Optional[list[str]]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    base = _openai_discovery_base_url(normalized)
    # Custom OpenAI-compatible endpoints serve a small curated catalog — use it verbatim. Official
    # OpenAI hosts (canonical and data-residency regional) return 120+ embeddings/whisper/tts/…
    # entries, so intersect with the curated agentic catalog so ``/model`` matches ``hermes model``.
    # Model not in live /v1/models — check the curated catalog before rejecting. Providers may omit models
    # from their live listing that are still valid (stale cache, partial rollout, gated previews). Use the
    # pure-catalog helper (no extra live fetch) so we only accept models Hermes actually ships. (#46850)
    # Their /v1/models listing is access-scoped and authoritative — a model absent from it is one this key
    # CANNOT serve, so the curated soft-accept would manufacture a selection that 400s at first use. Custom
    # OpenAI-compatible proxies keep the fallback (incomplete listings are common there).
    from hermes_cli.providers import is_official_openai_host

    try:
        live = fetch_api_models(api_key, base)
    except Exception:
        live = None
    if not live:
        return None
    if not is_official_openai_host(base):
        return live
    live_lower = {m.lower() for m in live}
    curated = list(_PROVIDER_MODELS.get(normalized, []))
    # Curated order, only models the account has access to; an account serving none of them (rare)
    # falls back to curated so the picker still offers sane defaults.
    discovered = [m for m in curated if m.lower() in live_lower]
    # Astra is intentionally absent from offline/static catalogs: the official API's
    # account-scoped /models response is the only source that may advertise it.
    discovered.extend(m for m in live if is_astra_model(m))
    return discovered or curated or live


def _custom_catalog(normalized: str, force_refresh: bool) -> Optional[list[str]]:
    base_url = _get_custom_base_url()
    if not base_url:
        return None
    model_cfg = _get_model_config_dict()
    # Try common API key env vars for custom endpoints.
    api_key = (
        str(model_cfg.get("api_key", "") or "").strip()
        or os.getenv("CUSTOM_API_KEY", "")
        or os.getenv("OPENAI_API_KEY", "")
        or os.getenv("OPENROUTER_API_KEY", ""))
    api_mode = "anthropic_messages" if _base_url_looks_like_anthropic_messages(base_url) else None
    return fetch_api_models(api_key, base_url, api_mode=api_mode) or None


def _bedrock_catalog(normalized: str, force_refresh: bool) -> Optional[list[str]]:
    # Live discovery keyed by the resolved AWS region so EU/AP users see eu.*/ap.* ids.
    try:
        from agent.bedrock_adapter import bedrock_model_ids_or_none

        return bedrock_model_ids_or_none()
    except Exception:
        return None


def _azure_foundry_catalog(normalized: str, force_refresh: bool) -> Optional[list[str]]:
    """Live ``GET <base>/models`` of the configured Azure Foundry resource (#27989).

    Deployments are per-resource, so the static catalog is intentionally empty and the plugin
    profile ships ``base_url=""`` — which is why the generic profile fetch never fires. Resolve
    through the runtime resolver so the picker targets the same resource inference hits
    (``model.base_url`` / ``AZURE_FOUNDRY_BASE_URL``) with the same credential: an API key string,
    or the Entra ID token-provider callable that ``azure_detect`` already accepts. Anthropic-style
    ``/anthropic`` routes serve no ``/models``; the probe never raises, so any miss keeps ``[]``.
    """
    try:
        from hermes_cli.azure_detect import _probe_openai_models
        from hermes_cli.runtime_provider import _resolve_azure_foundry_runtime

        runtime = _resolve_azure_foundry_runtime(requested_provider=normalized, model_cfg=_get_model_config_dict())
        base_url = str(runtime.get("base_url") or "").strip().rstrip("/")
        credential = runtime.get("api_key")
        if not (base_url and credential):
            return None
        ok, ids = _probe_openai_models(base_url, credential)
        return ids if ok and ids else None
    except Exception:
        return None


# Per-provider catalog sources tried before the generic profile fetch. A fetcher returning None
# falls through to the profile/curated path; a list is returned as-is (even empty).
_PROVIDER_CATALOG_FETCHERS: dict[str, Any] = {
    "openrouter": lambda normalized, force_refresh: model_ids(force_refresh=force_refresh),
    "openai-codex": _codex_catalog,
    "copilot": _copilot_catalog,
    "copilot-acp": _copilot_catalog,
    "nous": _nous_catalog,
    "stepfun": _api_key_provider_live,
    "gmi": _api_key_provider_live,
    "anthropic": _anthropic_catalog,
    "ai-gateway": lambda normalized, force_refresh: _fetch_ai_gateway_models() or None,
    # DeepInfra's generic /models mixes chat, image, video, speech and embedding models; the tagged
    # catalog helper is the only safe source for the chat picker, including its empty/failure result.
    "deepinfra": lambda normalized, force_refresh: _fetch_deepinfra_models(force_refresh=force_refresh) or [],
    "ollama-cloud": lambda normalized, force_refresh: fetch_ollama_cloud_models(force_refresh=force_refresh) or None,
    "openai": _openai_catalog,
    "openai-api": _openai_catalog,
    "custom": _custom_catalog,
    "bedrock": _bedrock_catalog,
    "azure-foundry": _azure_foundry_catalog}


# ``-free`` slugs the relay still LISTS but no longer serves: the Go-only twin (``ox-alpha-free``)
# and the promo it delisted without removing from ``/models`` (``deepseek-v4-flash-free``). The
# live-first keyed Zen/Go pickers filter through this so a stale live listing can never route
# into a 400/403 (#111749).
_OPENCODE_FREE_EXCLUDED_MODELS = frozenset(
    {"ox-alpha-free", "deepseek-v4-flash-free", "x-preview-f-free"}
)


def _profile_live_catalog(normalized: str) -> Optional[list[str]]:
    """Generic live fetch for any provider registered in providers/ with ``auth_type="api_key"``.

    Live results are merged with the curated list so models the live endpoint omits still appear:
    curated-first by default so the newest curated models lead when the live API lags;
    ``_LIVE_FIRST_PICKER_PROVIDERS`` (OpenCode Zen/Go, authoritative live API) live-first so stale
    curated entries stop polluting the top. Plugin providers without a static entry use the
    profile's ``fallback_models`` as the curated list (Fireworks lists an image model first).
    """
    from providers import get_provider_profile

    profile = get_provider_profile(normalized)
    if not profile:
        return None
    # external_process providers (ACP agent CLIs) have no api_key/base_url credentials: the
    # profile's fetch_models drives its own subprocess (kwargs are ignored per the base contract).
    # Every non-api-key profile falls back to its own fallback_models (OAuth plugins have no
    # static _PROVIDER_MODELS row), exactly as api_key plugins do below.
    if profile.auth_type == "external_process":
        try:
            live = profile.fetch_models()
        except Exception as exc:  # a failed subprocess launch degrades to the curated list, like api_key below
            logger.debug("external_process catalog fetch failed for %s: %s", normalized, exc)
            live = None
        # Same merge as setup (`_model_flow_plugin_provider`) so /model, the Desktop picker and
        # `hermes model` offer one list: live ids plus any pinned id the probe omitted.
        return merge_profile_catalog(normalized, profile, list(live) if live else None)
    if not (profile.auth_type == "api_key" and profile.base_url):
        return list(profile.fallback_models) or None
    api_key, base_url = _api_key_credentials(normalized)
    return probe_profile_catalog(normalized, profile, api_key, base_url or profile.base_url or None)


def probe_profile_catalog(normalized: str, profile, api_key: Optional[str], base_url: Optional[str]) -> Optional[list[str]]:
    """``profile.fetch_models`` gated on a key (no key → no doomed probe) and merged with the curated
    list; a raising catalog override degrades like a None return — fallback_models, not an empty picker."""
    live = None
    if api_key:
        try:
            live = profile.fetch_models(api_key=api_key, base_url=base_url)
        except Exception:
            live = None
    return merge_profile_catalog(normalized, profile, live)


def merge_profile_catalog(normalized: str, profile, live: Optional[list[str]]) -> Optional[list[str]]:
    """Combine a profile's live catalog with its curated list the way the ``/model`` picker does, so
    first-time setup (``model_setup_flows._api_key_provider_model_list``) offers the same rows the
    picker will later show. Empty live → ``fallback_models`` (None when the profile has none)."""
    if not live:
        rows = CuratedFallbackModels(profile.fallback_models) if profile.fallback_models else None
    else:
        curated = list(_PROVIDER_MODELS.get(normalized, [])) or list(profile.fallback_models or ())
        if not curated:
            rows = live
        else:
            primary, secondary = (live, curated) if normalized in _LIVE_FIRST_PICKER_PROVIDERS else (curated, live)
            rows = _merge_unique(primary, secondary, key=_model_dedup_key)
    return _drop_delisted_opencode_models(normalized, rows)


def _drop_delisted_opencode_models(normalized: str, rows: Optional[list[str]]) -> Optional[list[str]]:
    """The relay still LISTS delisted ids it no longer serves, and the curated floor (merged back in
    as the secondary half, or served alone when there is no key) carries retired ids too. Filter the
    FINAL rows for the live-first Zen/Go pickers so no path can offer a slug that 401s (#111749,
    #115496)."""
    if rows and normalized in _LIVE_FIRST_PICKER_PROVIDERS:
        return type(rows)(m for m in rows if str(m).lower() not in _OPENCODE_FREE_EXCLUDED_MODELS)
    return rows


def _chat_catalog_rows(models):
    """Drop generation ids from a chat-catalog list, keeping its list subclass."""
    from hermes_cli.chat_catalog import without_generation_models
    return without_generation_models(models)


def _configured_relay_base_url(provider: str) -> str:
    """``model.base_url`` when it points the *configured* provider at a relay/proxy, else "".

    Discovery must probe the same endpoint inference uses (#121387): when ``model.base_url``
    differs from the provider's own endpoint, the vendor's canonical host is NOT the catalog to list.
    Mirrors the ``$OPENAI_BASE_URL`` -> ``model.base_url`` -> canonical precedence of
    ``_openai_discovery_base_url`` for every built-in provider, not just OpenAI.
    """
    try:
        model_cfg = _get_model_config_dict()
    except Exception:
        return ""
    cfg_provider = str(model_cfg.get("provider") or "").strip().lower()
    if not cfg_provider or not provider:
        return ""
    try:
        normalized = normalize_provider(provider)
        if normalized != normalize_provider(cfg_provider):
            return ""
    except Exception:
        return ""
    base_url = str(model_cfg.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        return ""
    # A base_url equal to the provider's own endpoint is not a relay (setup persists canonical
    # URLs too): keep native discovery, which OAuth providers such as Codex need because the
    # generic relay probe only speaks api_key. Profiles cover providers PROVIDER_REGISTRY lacks
    # (OpenRouter).
    try:
        from providers import get_provider_profile

        canonical = getattr(get_provider_profile(normalized), "base_url", "") or ""
    except Exception:
        return base_url  # lookup failed: stay a relay, never widening where credentials go
    if canonical and normalize_route_base_url(base_url) == normalize_route_base_url(canonical):
        return ""
    return base_url


def _relay_model_catalog(normalized: str, relay: str) -> Optional[list[str]]:
    """Live catalog probed at a configured ``model.base_url`` relay, or None to fall through.

    Returns only the relay's live ids (no curated merge): a relay user must see the relay's
    catalog, and a failed/empty probe degrades to the canonical fetchers untouched.
    """
    try:
        from providers import get_provider_profile

        profile = get_provider_profile(normalized)
        if profile is None or getattr(profile, "auth_type", "") != "api_key":
            return None
        api_key, _ = _api_key_credentials(normalized)
        live = profile.fetch_models(api_key=api_key, base_url=relay)
        return [str(m) for m in (live or []) if m] or None
    except Exception:
        return None



# Canonical fetchers that already resolve `model.base_url` themselves for the configured
# provider AND degrade to their curated list when that relay fails — `_anthropic_catalog`,
# `_custom_catalog`, `_openai_catalog` (via `_openai_discovery_base_url`) and the simple
# api-key fetchers (via `resolve_api_key_provider_credentials`). They already satisfy the
# "no vendor egress when a relay is configured" invariant, so intercepting them would only
# override correct, better-merged behaviour. Everything else is vendor-pinned (#121387).
_RELAY_AWARE_CATALOG_FETCHERS = frozenset(
    {"anthropic", "custom", "openai", "openai-api", "stepfun", "gmi"}
)


def _static_catalog(normalized: str, fetcher: Any) -> list[str]:
    """The local, no-egress catalog tail: curated static list (+ models.dev merge where preferred).

    Shared by the normal path's final fallback and by the configured-relay degrade path, which
    must never reach a live vendor fetcher (#121387).
    """
    # Merge static curated list with live API results so models that the live endpoint omits (stale cache,
    # partial rollout) still appear in the picker. Single providers (kimi, zai) use curated-first (commit
    # 658ac1d86) to surface newest models even when live API lags (#46309). OpenCode Zen / Go are different:
    # their live API is the authoritative catalog, so they merge live-first — live entries lead and stale
    # curated entries no longer pollute the top of the picker. (#49129) Plugin providers with no static
    # _PROVIDER_MODELS entry fall back to the profile's curated fallback_models so their agentic picks lead
    # the picker instead of whatever the live catalog happens to return first (e.g. Fireworks lists an image
    # model, flux-*, ahead of its chat models).
    # A provider with a live fetcher that declined is serving a placeholder; one without any live
    # source is serving its authoritative catalog.
    curated_static = (CuratedFallbackModels if fetcher is not None else list)(_PROVIDER_MODELS.get(normalized, []))
    if normalized not in _MODELS_DEV_PREFERRED:
        return _chat_catalog_rows(_drop_delisted_opencode_models(normalized, curated_static))
    # models.dev keeps listing retired Zen ids too: filter after the merge, not before.
    merged = _drop_delisted_opencode_models(normalized, _merge_with_models_dev(normalized, curated_static))
    return _chat_catalog_rows(_xai_finalize_catalog(merged) if normalized in {"xai", "xai-oauth"} else merged)


def provider_model_ids(
    provider: Optional[str],
    *,
    force_refresh: bool = False,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    api_mode: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
) -> list[str]:
    """Return the best known model catalog for a provider.

    Tries live API endpoints for providers that support them (Codex, Nous),
    falling back to static lists. For providers in ``_MODELS_DEV_PREFERRED``
    (opencode-go/zen, xiaomi, deepseek, smaller inference providers, etc.),
    models.dev entries are merged on top of curated so new models released
    on the platform appear in ``/model`` without a Hermes release.
    """
    normalized = normalize_provider(provider)
    route_api_key = api_key
    route_base_url = base_url
    route_api_mode = api_mode
    route_headers = headers

    def _route_fetch_kwargs() -> dict[str, Any]:
        values: dict[str, Any] = {}
        if route_api_mode is not None:
            values["api_mode"] = route_api_mode
        if route_headers is not None:
            values["headers"] = route_headers
        return values

    if normalized == "moa":
        try:
            from hermes_cli.config import load_config
            from hermes_cli.moa_config import normalize_moa_config

            presets = normalize_moa_config(load_config().get("moa") or {}).get("presets") or {}
            preset_ids = list(presets)
        except Exception:
            preset_ids = []
        return _catalog_result(preset_ids, verified_models=preset_ids)
    if normalized == "copilot-acp":
        return _catalog_result(["copilot-acp"], verified_models=["copilot-acp"])
    if normalized == "openrouter":
        return model_ids(
            force_refresh=force_refresh,
            return_catalog=True,
            api_key=route_api_key,
            base_url=route_base_url,
            headers=route_headers,
        )
    if normalized == "openai-codex":
        from hermes_cli.codex_models import get_codex_model_ids

        # Pass the live OAuth access token so the picker matches whatever
        # ChatGPT lists for this account right now (new models appear without
        # a Hermes release). Falls back to the hardcoded catalog if no token
        # or the endpoint is unreachable.
        access_token = None
        try:
            from hermes_cli.auth import resolve_codex_runtime_credentials

            creds = resolve_codex_runtime_credentials(refresh_if_expiring=True)
            access_token = creds.get("api_key")
        except Exception:
            access_token = None
        return get_codex_model_ids(
            access_token=(
                route_api_key if route_api_key is not None else access_token
            ),
            base_url=route_base_url,
        )
    if normalized in {"copilot", "copilot-acp"}:
        try:
            live = _fetch_github_models(
                route_api_key
                if route_api_key is not None
                else _resolve_copilot_catalog_api_key()
            )
            if live:
                return _catalog_result(live, verified_models=live)
        except Exception:
            pass
        if normalized == "copilot-acp":
            return _catalog_result(list(_PROVIDER_MODELS.get("copilot", [])))
    if normalized == "nous":
        # Try live Nous Portal /models endpoint
        try:
            from hermes_cli.auth import fetch_nous_models, resolve_nous_runtime_credentials
            if route_api_key is not None or route_base_url is not None:
                creds = {
                    "api_key": route_api_key or "",
                    "base_url": route_base_url or "",
                }
            else:
                creds = resolve_nous_runtime_credentials()
            if creds:
                live = fetch_nous_models(api_key=creds.get("api_key", ""), inference_base_url=creds.get("base_url", ""))
                if live:
                    return _catalog_result(live, verified_models=live)
        except Exception:
            pass
        # Live failed (or no creds). Fall back to the docs-hosted manifest
        # — NOT the in-repo _PROVIDER_MODELS["nous"] snapshot — so newly
        # added Portal models still surface without a Hermes release.
        manifest_ids = get_curated_nous_model_ids()
        if manifest_ids:
            return _catalog_result(manifest_ids)
    if normalized == "stepfun":
        try:
            from hermes_cli.auth import resolve_api_key_provider_credentials

            creds = (
                {"api_key": route_api_key or "", "base_url": route_base_url or ""}
                if route_api_key is not None or route_base_url is not None
                else resolve_api_key_provider_credentials("stepfun")
            )
            api_key = str(creds.get("api_key") or "").strip()
            base_url = str(creds.get("base_url") or "").strip()
            if api_key and base_url:
                live = fetch_api_models(
                    api_key,
                    base_url,
                    **_route_fetch_kwargs(),
                )
                if live:
                    return _catalog_result(live, verified_models=live)
        except Exception:
            pass
    if normalized == "anthropic":
        if route_api_key is not None or route_base_url is not None:
            cfg_base_url = str(route_base_url or "").strip()
            cfg_api_key = str(route_api_key or "").strip()
        else:
            model_cfg = _get_model_config_dict()
            cfg_provider = normalize_provider(str(model_cfg.get("provider", "") or ""))
            if cfg_provider == "anthropic":
                cfg_base_url = str(model_cfg.get("base_url", "") or "").strip()
                cfg_api_key = str(model_cfg.get("api_key", "") or "").strip()
            else:
                cfg_base_url = ""
                cfg_api_key = ""
        live = _fetch_anthropic_models(
            base_url=cfg_base_url or None,
            api_key=cfg_api_key or None,
        )
        if live:
            if cfg_base_url:
                return _catalog_result(live, verified_models=live)
            # The live /v1/models dump lags newly-routed curated aliases
            # (e.g. claude-fable-5, which is reachable on Anthropic before it
            # is enumerated by the models endpoint). Surface curated entries
            # first, then append any live-only models, so a fresh curated
            # model never disappears just because the API hasn't listed it yet.
            curated = list(_PROVIDER_MODELS.get("anthropic", []))
            merged = list(curated)
            merged_lower = {m.lower() for m in curated}
            for m in live:
                if m.lower() not in merged_lower:
                    merged.append(m)
                    merged_lower.add(m.lower())
            return _catalog_result(
                merged,
                verified_models=live,
            )
        return _catalog_result(list(_PROVIDER_MODELS.get("anthropic", [])))
    if normalized == "ai-gateway":
        live = _fetch_ai_gateway_models(
            api_key=route_api_key,
            base_url=route_base_url,
        )
        if live:
            return _catalog_result(live, verified_models=live)
    if normalized == "deepinfra":
        # DeepInfra's generic /models endpoint mixes chat, image, video,
        # speech, and embedding models. The tagged catalog helper is the only
        # safe source for the chat picker, including its empty/failure result.
        ids = _fetch_deepinfra_models(
            force_refresh=force_refresh,
            api_key=route_api_key,
            base_url=route_base_url,
        ) or []
        return _catalog_result(ids, verified_models=ids)
    if normalized == "ollama-cloud":
        return fetch_ollama_cloud_models(
            api_key=route_api_key,
            base_url=route_base_url,
            force_refresh=force_refresh,
            return_catalog=True,
        )
    if normalized in ("openai", "openai-api"):
        discovery_api_key = (
            str(route_api_key).strip()
            if route_api_key is not None
            else os.getenv("OPENAI_API_KEY", "").strip()
        )
        if discovery_api_key:
            base = (
                str(route_base_url).strip()
                if route_base_url is not None
                else _openai_discovery_base_url(normalized)
            )
            # Custom OpenAI-compatible endpoints (proxies, gateways, self-hosted)
            # may serve a small curated catalog — use the live list verbatim so
            # discovery works. But the official OpenAI hosts (canonical AND the
            # data-residency regional hosts, which serve the identical dump)
            # return 120+ entries of embeddings, whisper, tts, dall-e,
            # moderation and legacy chat models — none of which belong in the
            # agent model picker. For official hosts, intersect the live list
            # with our curated agentic catalog so ``/model`` matches what
            # ``hermes model`` shows.
            from hermes_cli.providers import is_official_openai_host

            is_default_openai = is_official_openai_host(base)
            try:
                live = fetch_api_models(
                    discovery_api_key,
                    base,
                    **_route_fetch_kwargs(),
                )
                if live:
                    if is_default_openai:
                        live_lower = {m.lower() for m in live}
                        curated = list(_PROVIDER_MODELS.get(normalized, []))
                        # Keep curated order; only surface curated models the
                        # account actually has access to.
                        filtered = [m for m in curated if m.lower() in live_lower]
                        if filtered:
                            return _catalog_result(filtered, verified_models=filtered)
                        # Account serves none of the curated models (rare —
                        # e.g. org without GPT-5 access). Fall back to curated
                        # so the picker still offers sane defaults.
                        return _catalog_result(curated or live, verified_models=live)
                    return _catalog_result(live, verified_models=live)
            except Exception:
                pass
    if normalized == "gmi":
        try:
            from hermes_cli.auth import resolve_api_key_provider_credentials

            creds = (
                {"api_key": route_api_key or "", "base_url": route_base_url or ""}
                if route_api_key is not None or route_base_url is not None
                else resolve_api_key_provider_credentials("gmi")
            )
            api_key = str(creds.get("api_key") or "").strip()
            base_url = str(creds.get("base_url") or "").strip()
            if api_key and base_url:
                live = fetch_api_models(
                    api_key,
                    base_url,
                    **_route_fetch_kwargs(),
                )
                if live:
                    return _catalog_result(live, verified_models=live)
        except Exception:
            pass
    if normalized == "custom":
        base_url = (
            str(route_base_url).strip()
            if route_base_url is not None
            else _get_custom_base_url()
        )
        if base_url:
            model_cfg = _get_model_config_dict()
            # Try common API key env vars for custom endpoints
            api_key = (
                str(route_api_key).strip()
                if route_api_key is not None
                else (
                    str(model_cfg.get("api_key", "") or "").strip()
                    or os.getenv("CUSTOM_API_KEY", "")
                    or os.getenv("OPENAI_API_KEY", "")
                    or os.getenv("OPENROUTER_API_KEY", "")
                )
            )
            api_mode = route_api_mode or (
                "anthropic_messages"
                if _base_url_looks_like_anthropic_messages(base_url)
                else None
            )
            custom_fetch_kwargs = {}
            if api_mode is not None:
                custom_fetch_kwargs["api_mode"] = api_mode
            if route_headers is not None:
                custom_fetch_kwargs["headers"] = route_headers
            live = fetch_api_models(
                api_key,
                base_url,
                **custom_fetch_kwargs,
            )
            if live:
                return live
    # Bedrock uses live discovery keyed by the resolved AWS region so that
    # EU/AP users see eu.*/ap.* model IDs instead of the static us.* list.
    # Note: early return intentionally skips _MODELS_DEV_PREFERRED merge
    # below — bedrock is not expected to appear in that table.
    if normalized == "bedrock":
        try:
            from agent.bedrock_adapter import bedrock_model_ids_or_none
            ids = bedrock_model_ids_or_none()
            if ids is not None:
                return _catalog_result(ids, verified_models=ids)
        except Exception:
            pass

    # ── Profile-based generic live fetch (all simple api-key providers) ──
    # Handles any provider registered in providers/ with auth_type="api_key".
    # Replaces per-provider copy-paste blocks (stepfun, gmi, zai, etc.).
    try:
        from providers import get_provider_profile
        from hermes_cli.auth import resolve_api_key_provider_credentials

        _p = get_provider_profile(normalized)
        if _p and _p.auth_type == "api_key" and _p.base_url:
            if route_api_key is not None or route_base_url is not None:
                api_key = str(route_api_key or "").strip()
                base_url = str(route_base_url or "").strip()
            else:
                try:
                    creds = resolve_api_key_provider_credentials(normalized)
                    api_key = str(creds.get("api_key") or "").strip()
                    base_url = str(creds.get("base_url") or "").strip()
                except Exception:
                    api_key, base_url = "", _p.base_url
            if not base_url:
                base_url = _p.base_url
            if api_key:
                live = _p.fetch_models(api_key=api_key, base_url=base_url or None)
                if live:
                    # Merge static curated list with live API results so
                    # models that the live endpoint omits (stale cache,
                    # partial rollout) still appear in the picker.
                    #
                    # Single providers (kimi, zai) use curated-first
                    # (commit 658ac1d86) to surface newest models even when live
                    # API lags (#46309). OpenCode Zen / Go are different: their
                    # live API is the authoritative catalog, so they merge
                    # live-first — live entries lead and stale curated entries
                    # no longer pollute the top of the picker. (#49129)
                    #
                    # Plugin providers with no static _PROVIDER_MODELS entry fall
                    # back to the profile's curated fallback_models so their
                    # agentic picks lead the picker instead of whatever the live
                    # catalog happens to return first (e.g. Fireworks lists an
                    # image model, flux-*, ahead of its chat models).
                    curated = list(_PROVIDER_MODELS.get(normalized, [])) or list(
                        _p.fallback_models or ()
                    )
                    if curated:
                        if normalized in _LIVE_FIRST_PICKER_PROVIDERS:
                            primary, secondary = live, curated
                        else:
                            primary, secondary = curated, live
                        merged = list(primary)
                        merged_lower = {_model_dedup_key(m) for m in primary}
                        for m in secondary:
                            if _model_dedup_key(m) not in merged_lower:
                                merged.append(m)
                                merged_lower.add(_model_dedup_key(m))
                        return _catalog_result(merged, verified_models=live)
                    return _catalog_result(live, verified_models=live)
            # Use profile's fallback_models if defined
            if _p.fallback_models:
                return _catalog_result(list(_p.fallback_models))
    except Exception:
        pass

    curated_static = list(_PROVIDER_MODELS.get(normalized, []))
    if normalized in _MODELS_DEV_PREFERRED:
        merged = _merge_with_models_dev(normalized, curated_static)
        if normalized in {"xai", "xai-oauth"}:
            return _catalog_result(_xai_finalize_catalog(merged))
        return _catalog_result(merged)
    return _catalog_result(curated_static)


# ---------------------------------------------------------------------------
# Disk cache for provider_model_ids() — keeps /model picker fast (otherwise every open re-fetches
# every authed provider's /v1/models). One JSON file at $HERMES_HOME/provider_models_cache.json;
# entries keyed by credential fingerprint (rotate OPENAI_API_KEY → entry invalidates); 1h TTL;
# only NON-EMPTY results are cached so a transient failure is never pinned; any read/write error
# degrades silently to a live fetch.
# ---------------------------------------------------------------------------

_PROVIDER_MODELS_CACHE_TTL = 3600  # 1h
# Stale-while-revalidate window: an expired same-credentials entry is served IMMEDIATELY while a
# daemon thread refreshes the disk cache; beyond this bound the caller blocks on a live fetch.
# Catalogs change on release timescales, so hour-old data beats stalling every picker surface.
_PROVIDER_MODELS_STALE_SERVE_MAX = 7 * 24 * 3600  # 7d
# A curated fallback row is a placeholder for an outage, not a catalog: re-probe soon and never
# serve it through the stale window.
_PROVIDER_MODELS_FALLBACK_TTL = 60

# Cache keys with a background SWR refresh in flight — dedupes concurrent refreshes.
_swr_refresh_inflight: set = set()
_swr_refresh_lock = threading.Lock()


def _cache_entry(fp: str, models: list[str], at: Optional[float] = None) -> dict:
    """One provider row of the disk cache: credential fingerprint, write time, model ids."""
    return {"fp": fp, "at": time.time() if at is None else at, "models": list(models)}


def _live_result_entry(fp: str, live: list[str], existing: Any, at: Optional[float] = None) -> Optional[dict]:
    """Row to store for a ``provider_model_ids`` result, or ``None`` to keep *existing*: a curated
    fallback never replaces the account's real catalog for the same credentials, and when it is
    stored it is flagged so it expires on the short fallback TTL."""
    if not isinstance(live, CuratedFallbackModels):
        return _cache_entry(fp, live, at)
    if _cache_entry_valid(existing, fp) and not existing.get("fallback"):
        return None
    return {**_cache_entry(fp, live, at), "fallback": True}


def _ollama_native_probe_reachable() -> bool:
    """Whether the configured local Ollama root answered the native ``/api/tags`` probe (an empty
    catalog from a reachable server is authoritative; a failed probe is not)."""
    base_url = _get_ollama_base_url()
    headers = _get_ollama_native_headers(base_url) or None
    probe_key = _ollama_probe_cache_key(_root_for_ollama_native_api(base_url), headers)
    return _OLLAMA_LOCAL_PROBE_REACHABLE.get(probe_key) is True


def _spawn_swr_refresh(cache_key: str, refresh_fn=None) -> None:
    """Fire-and-forget daemon refresh of *cache_key*'s cache entry, at most one in flight per key.
    Failures are swallowed — the stale entry stays served until a later refresh succeeds.
    ``refresh_fn`` (no-args → fresh entry dict or None) lets ``custom:<base_url>`` keys from
    :func:`cached_fetch_api_models` reuse the same inflight-dedupe scaffolding."""
    # Under a routed profile the inflight key includes the home: the same provider slug names a
    # different disk cache and credential set per profile, so one profile's refresh must not
    # suppress another's. Unscoped keeps the bare key (tests inspect the set by slug).
    from hermes_constants import get_hermes_home_override, hermes_home_key
    inflight_key = cache_key if get_hermes_home_override() is None else (hermes_home_key(), cache_key)
    with _swr_refresh_lock:
        if inflight_key in _swr_refresh_inflight:
            return
        _swr_refresh_inflight.add(inflight_key)

    def _default_refresh():
        live = provider_model_ids(cache_key, force_refresh=True)
        if live or (cache_key == "ollama" and _ollama_native_probe_reachable()):
            fp = _credential_fingerprint(cache_key)
            return _live_result_entry(fp, live or [], _load_provider_models_cache().get(cache_key))
        return None

    def _refresh() -> None:
        try:
            entry = (refresh_fn or _default_refresh)()
            if entry:
                # Under the write lock: the GUI read path spawns one of these per stale provider, so
                # the plain load-modify-save would let concurrent warms drop each other's rows.
                with _cache_write_lock:
                    _store_cache_entry(cache_key, entry)
        except Exception:
            logger.debug("SWR refresh failed for %s", cache_key, exc_info=True)
        finally:
            with _swr_refresh_lock:
                _swr_refresh_inflight.discard(inflight_key)

    # copy_context: the refresh must read the calling profile's credentials and write ITS disk cache.
    ctx = contextvars.copy_context()
    threading.Thread(target=lambda: ctx.run(_refresh), daemon=True, name=f"model-cache-swr-{cache_key}").start()


def _provider_models_cache_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "provider_models_cache.json"


def _credential_fingerprint(
    provider: str,
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    api_mode: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
) -> str:
    """Return a short hash representing the credentials that
    ``provider_model_ids(provider)`` would see right now.

    Rotating any of the relevant env vars invalidates the cached entry
    for that provider. We hash AT LEAST the api-key + base-url env vars
    declared in ``PROVIDER_REGISTRY``. For OAuth-backed providers
    (codex, copilot, anthropic-via-claude-code, nous portal), the
    relevant tokens live in ``$HERMES_HOME/auth.json`` and external
    credential files. Rather than parse every shape, we additionally
    fold the mtime of those files into the fingerprint so refreshes
    after re-auth bust the cache.
    """
    import hashlib
    import os as _os

    parts: list[str] = []
    if any(value is not None for value in (api_key, base_url, api_mode, headers)):
        parts.extend(
            [
                f"route_api_key={api_key or ''}",
                f"route_base_url={base_url or ''}",
                f"route_api_mode={api_mode or ''}",
                f"route_headers={json.dumps(headers or {}, sort_keys=True)}",
            ]
        )

    # Env vars from PROVIDER_REGISTRY for this slug
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
        pcfg = PROVIDER_REGISTRY.get(provider)
        if pcfg is not None:
            for ev in getattr(pcfg, "api_key_env_vars", ()) or ():
                parts.append(f"{ev}={_os.environ.get(ev, '')}")
            bev = getattr(pcfg, "base_url_env_var", "") or ""
            if bev:
                parts.append(f"{bev}={_os.environ.get(bev, '')}")
    except Exception:
        pass

    # Effective configured endpoint: config.yaml's model.base_url changes the
    # endpoint discovery probes (data-residency hosts) without touching any
    # env var, so it must change the fingerprint too or `hermes config set
    # model.base_url ...` keeps serving the previous endpoint's cached
    # catalog until TTL expiry.
    if provider in ("openai", "openai-api"):
        try:
            parts.append(f"effective_base={_openai_discovery_base_url(provider)}")
        except Exception:
            pass

    # OAuth / external-file mtimes that change on re-auth
    try:
        from hermes_constants import get_hermes_home
        for rel in ("auth.json", "credentials.json"):
            p = get_hermes_home() / rel
            try:
                parts.append(f"{rel}@{p.stat().st_mtime_ns}")
            except FileNotFoundError:
                parts.append(f"{rel}@missing")
            except Exception:
                pass
    except Exception:
        pass

    # External well-known credential file locations
    for path in (
        _os.path.expanduser("~/.codex/auth.json"),
        _os.path.expanduser("~/.claude/.credentials.json"),
        _os.path.expanduser("~/.config/github-copilot/hosts.json"),
        _os.path.expanduser("~/.minimax/credentials.json"),
    ):
        try:
            mt = _os.stat(path).st_mtime_ns
            parts.append(f"{path}@{mt}")
        except FileNotFoundError:
            parts.append(f"{path}@missing")
        except Exception:
            pass

    blob = "|".join(parts).encode("utf-8", errors="replace")
    # blake2b for cache-key fingerprinting only — not for credential storage.
    # We never reverse this hash; collisions are harmless (worst case: cache
    # miss → live re-fetch). Use blake2b instead of sha256 here because
    # CodeQL's `py/weak-sensitive-data-hashing` rule flags sha256 over env
    # vars whose names contain "API_KEY" / "TOKEN" even when the hash is
    # used as an identity fingerprint, not for password storage. blake2b
    # is a keyed-hash primitive and isn't flagged.
    return hashlib.blake2b(blob, digest_size=8).hexdigest()


def _load_provider_models_cache() -> dict:
    """Return the full cache dict, or {} on any error."""
    try:
        return _read_json_cache(_provider_models_cache_path()) or {}
    except Exception:
        return {}


_cache_write_lock = threading.Lock()


def _save_provider_models_cache(data: dict) -> None:
    """Persist the cache dict. Best-effort — silent on any error."""
    try:
        _write_json_cache(_provider_models_cache_path(), data, indent=None)
    except Exception:
        pass


def _store_cache_entry(cache_key: str, entry: dict, cache: Optional[dict] = None) -> None:
    """Write one row into the disk cache (reloading the latest state unless ``cache`` is given)."""
    if cache is None:
        cache = _load_provider_models_cache()
    cache[cache_key] = entry
    _save_provider_models_cache(cache)


def update_provider_cache_entry(provider: str, models: list[str]) -> None:
    """Thread-safe single-entry update for parallel prefetch workers: load-modify-save under a lock
    so concurrent fetches don't clobber each other's rows. Best-effort, silent on any error."""
    try:
        normalized = normalize_provider(provider) or (provider or "")
        if not normalized or not models:
            return
        fp = _credential_fingerprint(normalized)
        with _cache_write_lock:
            _store_cache_entry(normalized, _cache_entry(fp, models))
    except Exception:
        pass


def _normalized_cache_slug(provider: Optional[str]) -> str:
    """``ollama`` stays a raw slug (its alias would canonicalize to ``custom``); everything else normalizes."""
    requested = str(provider or "").strip().lower()
    return requested if requested == "ollama" else (normalize_provider(provider) or (provider or ""))


def _model_requires_account_discovery(provider: Optional[str], model: str) -> bool:
    """Astra names cannot confer API/OAuth entitlement through picker state."""
    return _normalized_cache_slug(provider) in {"openai", "openai-api", "openai-codex"} and is_astra_model(model)


def cached_provider_model_ids(
    provider: Optional[str],
    *,
    force_refresh: bool = False,
    cache_only: bool = False,
    require_verified: bool = False,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    api_mode: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
    ttl_seconds: int = _PROVIDER_MODELS_CACHE_TTL,
    non_blocking: bool = False,
) -> list[str]:
    """Disk-cached wrapper around :func:`provider_model_ids`.

    Hits the cache when fresh; otherwise calls the live function and
    persists a non-empty result. Always returns a list (never None). With
    ``cache_only=True`` it returns only a usable cached entry and never
    invokes the live provider resolver. ``require_verified=True`` narrows the
    result to model IDs confirmed by a live/account catalog; it is used by
    delegation admission, not ordinary picker callers. ``non_blocking`` is
    the GUI read path: serve a same-credentials stale row or an empty list,
    and warm the catalog off-thread rather than probing in the caller.
    """
    normalized = normalize_provider(provider) or (provider or "")
    if not normalized:
        return []

    cache = _load_provider_models_cache()
    fp = _credential_fingerprint(
        normalized,
        api_key=api_key,
        base_url=base_url,
        api_mode=api_mode,
        headers=headers,
    )
    entry = cache.get(normalized)
    now = time.time()

    def _verified_entry_models(cache_entry) -> list[str]:
        values = cache_entry.get("verified_models") if isinstance(cache_entry, dict) else None
        return [str(model) for model in values] if isinstance(values, list) else []

    if cache_only:
        if (
            not force_refresh
            and _cache_entry_valid(entry, fp)
            and now - entry["at"] < _PROVIDER_MODELS_STALE_SERVE_MAX
        ):
            models = list(entry["models"])
            return (
                [model for model in models if model in set(_verified_entry_models(entry))]
                if require_verified else models
            )
        return []

    if not force_refresh and _cache_entry_valid(entry, fp):
        age = now - entry["at"]
        if age < ttl_seconds:
            models = list(entry["models"])
            return (
                [model for model in models if model in set(_verified_entry_models(entry))]
                if require_verified else models
            )
        if age < _PROVIDER_MODELS_STALE_SERVE_MAX:
            # Stale-while-revalidate: serve the expired entry immediately so
            # interactive picker opens never block on serial /v1/models
            # round-trips; refresh the cache off-thread for the next open.
            _spawn_swr_refresh(normalized)
            models = list(entry["models"])
            return (
                [model for model in models if model in set(_verified_entry_models(entry))]
                if require_verified else models
            )

    if non_blocking and not force_refresh:
        _spawn_swr_refresh(normalized)
        if _cache_entry_valid(entry, fp, allow_empty=normalized == "ollama"):
            models = [
                model for model in entry["models"]
                if not _model_requires_account_discovery(normalized, model)
            ]
            if require_verified:
                models = [model for model in models if model in set(_verified_entry_models(entry))]
            return _chat_catalog_rows(models)
        return []

    # Cache miss / stale / forced refresh — call the live path.
    live_kwargs = {"force_refresh": force_refresh}
    if any(value is not None for value in (api_key, base_url, api_mode, headers)):
        live_kwargs.update(
            {
                "api_key": api_key,
                "base_url": base_url,
                "api_mode": api_mode,
                "headers": headers,
            }
        )
    live = provider_model_ids(normalized, **live_kwargs)
    verified_models = list(getattr(live, "verified_models", ()) or ())
    if live:
        cache[normalized] = {
            "fp": fp,
            "at": now,
            "models": list(live),
            "verified_models": verified_models,
        }
        _save_provider_models_cache(cache)
    if require_verified:
        if not verified_models:
            return []
        return [model for model in live if model in set(verified_models)]
    if live:
        return list(live)

    # Live fetch returned nothing. If we have a stale entry with the
    # SAME fingerprint, prefer it over an empty result — stale data
    # beats no data when the network is flaky.
    if _cache_entry_valid(entry, fp):
        return list(entry["models"])
    return list(live or [])


def clear_provider_models_cache(provider: Optional[str] = None) -> None:
    """Drop one provider's cache entry, or wipe the whole cache (``provider=None``). Used by
    ``/model --refresh`` and ``hermes model --refresh``."""
    try:
        # Native Ollama tags are keyed by root URL, not provider slug — a targeted refresh can't
        # identify the root from the name alone, so clear this small in-process cache every time.
        _OLLAMA_LOCAL_MODELS_CACHE.clear()
        _OLLAMA_LOCAL_PROBE_FAILURE_CACHE.clear()
        _OLLAMA_LOCAL_PROBE_REACHABLE.clear()
        # A fresh copilot-acp CLI login must be visible to the next /model switch (this helper is
        # what ``--refresh`` runs): don't let the 5-min session memo (or its failure memo) serve
        # a stale signed-out probe past an explicit refresh.
        global _copilot_acp_session_memo
        _copilot_acp_session_memo = None
        if provider is None:
            path = _provider_models_cache_path()
            if path.exists():
                path.unlink()
            return
        cache = _load_provider_models_cache()
        normalized = _normalized_cache_slug(provider)
        if normalized in cache:
            del cache[normalized]
            _save_provider_models_cache(cache)
    except Exception:
        pass


def _resolve_anthropic_pool_catalog_credentials() -> tuple[str, str]:
    """Read-only API-key pool credential for model discovery (``resolve_anthropic_token()`` ignores
    ``api_key`` pool entries — its runtime contract is OAuth-oriented)."""
    try:
        from agent.credential_pool import AUTH_TYPE_API_KEY
        from hermes_cli.auth import read_credential_pool

        for entry in read_credential_pool("anthropic"):
            if not isinstance(entry, dict) or entry.get("auth_type") != AUTH_TYPE_API_KEY:
                continue
            token = str(entry.get("access_token") or "").strip()
            if token:
                return token, str(entry.get("base_url") or entry.get("inference_base_url") or "").strip()
    except Exception:
        pass
    return "", ""


def _fetch_anthropic_models(
    timeout: float = 5.0, *, base_url: Optional[str] = None, api_key: Optional[str] = None
) -> Optional[list[str]]:
    """Sorted model ids from the Anthropic /v1/models endpoint, or None. Credentials: explicit
    ``api_key``, else ``resolve_anthropic_token()`` (env / OAuth / Claude Code), else a read-only
    API-key credential_pool entry."""
    try:
        from agent.anthropic_credentials import resolve_anthropic_token, _is_oauth_token
    except ImportError:
        return None

    resolved_base_url = base_url
    token = (api_key or "").strip() or resolve_anthropic_token()
    if not token:
        # A pool credential and its endpoint are one security boundary — never pair the pool key
        # with a caller-provided endpoint.
        token, resolved_base_url = _resolve_anthropic_pool_catalog_credentials()
    if not token:
        return None

    headers: dict[str, str] = {"anthropic-version": "2023-06-01"}
    is_oauth = _is_oauth_token(token)
    if is_oauth:
        headers["Authorization"] = f"Bearer {token}"
        from agent.anthropic_adapter import _COMMON_BETAS, _OAUTH_ONLY_BETAS, _CONTEXT_1M_BETA
        headers["anthropic-beta"] = ",".join(_COMMON_BETAS + _OAUTH_ONLY_BETAS)
    else:
        headers["x-api-key"] = token

    url = _anthropic_models_url(resolved_base_url)
    try:
        try:
            data = _get_json(url, timeout=timeout, headers=headers)
        except urllib.error.HTTPError as http_err:
            # OAuth subscriptions that 400 the 1M context beta ("long context beta is not yet
            # available for this subscription"): retry once without it; re-raise anything else.
            if not (is_oauth and http_err.code == 400):
                raise
            try:
                body_text = http_err.read().decode(errors="ignore").lower()
            except Exception:
                body_text = ""
            if not ("long context beta" in body_text and "not yet available" in body_text):
                raise
            headers["anthropic-beta"] = ",".join(
                [b for b in _COMMON_BETAS if b != _CONTEXT_1M_BETA] + list(_OAUTH_ONLY_BETAS)
            )
            data = _get_json(url, timeout=timeout, headers=headers)
        models = [m["id"] for m in data.get("data", []) if m.get("id")]
        seen_cursors: set[str] = set()
        for _page in range(_ANTHROPIC_MODELS_MAX_PAGES):
            cursor = _anthropic_next_cursor(data, seen_cursors)
            if cursor is None:
                break
            data = _get_json(_anthropic_models_url(resolved_base_url, after_id=cursor), timeout=timeout, headers=headers)
            models.extend(m["id"] for m in data.get("data", []) if m.get("id"))
        models = list(dict.fromkeys(models))
        # opus, then sonnet, then haiku; alphabetical within tier.
        return sorted(models, key=lambda m: ("opus" not in m, "sonnet" not in m, "haiku" not in m, m))
    except Exception as e:
        logger.debug("Failed to fetch Anthropic models: %s", e)
        return None


def _payload_items(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data", []) if isinstance(payload, dict) else payload
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def copilot_default_headers(*, is_agent_turn: bool = True) -> dict[str, str]:
    """Standard headers for Copilot API requests."""
    try:
        from hermes_cli.copilot_auth import copilot_request_headers
        return copilot_request_headers(is_agent_turn=is_agent_turn)
    except ImportError:
        return {
            "Editor-Version": COPILOT_EDITOR_VERSION,
            "User-Agent": "HermesAgent/1.0",
            "Openai-Intent": "conversation-edits",
            "x-initiator": "agent" if is_agent_turn else "user"}


_COPILOT_CHAT_ENDPOINTS = {"/chat/completions", "/responses", "/v1/messages"}


def _copilot_catalog_item_is_text_model(
    item: dict[str, Any], *, ignore_picker_flag: bool = False) -> bool:
    if not str(item.get("id") or "").strip():
        return False
    if not ignore_picker_flag and item.get("model_picker_enabled") is False:
        return False
    capabilities = item.get("capabilities")
    if isinstance(capabilities, dict):
        model_type = str(capabilities.get("type") or "").strip().lower()
        if model_type and model_type != "chat":
            return False
    supported_endpoints = item.get("supported_endpoints")
    if isinstance(supported_endpoints, list):
        endpoints = {e for endpoint in supported_endpoints if (e := str(endpoint).strip())}
        if endpoints and not endpoints & _COPILOT_CHAT_ENDPOINTS:
            return False
    return True


def _copilot_text_models(items: list[dict[str, Any]], *, ignore_picker_flag: bool = False) -> list[dict[str, Any]]:
    """Chat-capable catalog rows, deduped by id, in catalog order."""
    models: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in items:
        model_id = str(item.get("id") or "").strip()
        if model_id in seen_ids:
            continue
        if not _copilot_catalog_item_is_text_model(item, ignore_picker_flag=ignore_picker_flag):
            continue
        seen_ids.add(model_id)
        models.append(item)
    return models


# Short-TTL cache of the filtered GitHub Copilot /models catalog (picker + context/normalize helpers
# share it). Keyed by the api_key of the successful fetch so a credential swap never serves the
# previous account's catalog; monotonic clock; lock-free (a race at worst duplicates one fetch).
_github_model_catalog_cache: Optional[list[dict[str, Any]]] = None
_github_model_catalog_cache_key: Optional[str] = None
_github_model_catalog_cache_time: float = 0.0
_GITHUB_MODEL_CATALOG_CACHE_TTL = 300  # 5 minutes


def fetch_github_model_catalog(
    api_key: Optional[str] = None, timeout: float = 5.0) -> Optional[list[dict[str, Any]]]:
    """Fetch the live GitHub Copilot model catalog for this account."""
    global _github_model_catalog_cache, _github_model_catalog_cache_key
    global _github_model_catalog_cache_time

    if (
        _github_model_catalog_cache is not None
        and _github_model_catalog_cache_key == api_key
        and (time.monotonic() - _github_model_catalog_cache_time) < _GITHUB_MODEL_CATALOG_CACHE_TTL
    ):
        return copy.deepcopy(_github_model_catalog_cache)  # deep: callers must not mutate cached dicts

    attempts: list[dict[str, str]] = []
    if api_key:
        attempts.append({**copilot_default_headers(), "Authorization": f"Bearer {api_key}"})
    attempts.append(copilot_default_headers())

    for headers in attempts:
        try:
            items = _payload_items(_get_json(COPILOT_MODELS_URL, timeout=timeout, headers=headers))
        except Exception:
            continue
        models = _copilot_text_models(items)
        if not models and items:
            # GitHub has been observed returning ``model_picker_enabled: false`` for EVERY model on
            # some accounts, which would strand the picker on the stale curated fallback. The flag
            # is a display hint, not an availability contract — retry without it (chat/endpoint
            # checks still apply).
            models = _copilot_text_models(items, ignore_picker_flag=True)
        if models:
            _github_model_catalog_cache = copy.deepcopy(models)
            _github_model_catalog_cache_key = api_key
            _github_model_catalog_cache_time = time.monotonic()
            return models
    return None


# ─── Copilot catalog context-window helpers ───

# Module-level cache: {model_id: max_prompt_tokens}
_copilot_context_cache: dict[str, int] = {}
_copilot_context_cache_time: float = 0.0
_copilot_context_cache_key: Optional[str] = None  # fingerprint of the api_key the entry was fetched with
_COPILOT_CONTEXT_CACHE_TTL = 3600  # 1 hour


def get_copilot_model_context(model_id: str, api_key: Optional[str] = None) -> Optional[int]:
    """``max_prompt_tokens`` for a Copilot model from the live /models API (cached in-process 1h; a
    miss on a fresh cache does not re-fetch), or None."""
    global _copilot_context_cache, _copilot_context_cache_time, _copilot_context_cache_key

    # Keyed on the credential like fetch_github_model_catalog: the catalog (and its limits) is
    # per-account, so another profile's token must not be served this entry.
    from agent.credential_persistence import fingerprint_secret_value
    key_fp = fingerprint_secret_value(api_key)
    if (_copilot_context_cache and _copilot_context_cache_key == key_fp
            and (time.time() - _copilot_context_cache_time < _COPILOT_CONTEXT_CACHE_TTL)):
        return _copilot_context_cache.get(model_id)

    catalog = fetch_github_model_catalog(api_key=api_key)
    if not catalog:
        return None
    cache: dict[str, int] = {}
    for item in catalog:
        mid = str(item.get("id") or "").strip()
        max_prompt = ((item.get("capabilities") or {}).get("limits") or {}).get("max_prompt_tokens")
        if mid and isinstance(max_prompt, int) and max_prompt > 0:
            cache[mid] = max_prompt
    _copilot_context_cache = cache
    _copilot_context_cache_time = time.time()
    _copilot_context_cache_key = key_fp
    return cache.get(model_id)


def _is_github_models_base_url(base_url: Optional[str]) -> bool:
    return (base_url or "").strip().rstrip("/").lower().startswith(
        (COPILOT_BASE_URL, "https://models.github.ai/inference", "https://models.inference.ai.azure.com")
    )


def _fetch_github_models(api_key: Optional[str] = None, timeout: float = 5.0) -> Optional[list[str]]:
    catalog = fetch_github_model_catalog(api_key=api_key, timeout=timeout)
    return [item.get("id", "") for item in catalog if item.get("id")] if catalog else None


def _copilot_catalog_ids(
    catalog: Optional[list[dict[str, Any]]] = None, api_key: Optional[str] = None) -> set[str]:
    if catalog is None and api_key:
        catalog = fetch_github_model_catalog(api_key=api_key)
    return {mid for item in (catalog or []) if (mid := str(item.get("id") or "").strip())}


def normalize_copilot_model_id(
    model_id: Optional[str], *, catalog: Optional[list[dict[str, Any]]] = None,
    api_key: Optional[str] = None) -> str:
    raw = str(model_id or "").strip()
    if not raw:
        return ""

    catalog_ids = _copilot_catalog_ids(catalog=catalog, api_key=api_key)
    alias = _COPILOT_MODEL_ALIASES.get(raw)
    if alias:
        return alias

    candidates = [raw]
    if "/" in raw:
        candidates.append(raw.split("/", 1)[1].strip())
    if raw.endswith(("-mini", "-nano", "-chat")):
        candidates.append(raw[:-5])

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if candidate in _COPILOT_MODEL_ALIASES:
            return _COPILOT_MODEL_ALIASES[candidate]
        if candidate in catalog_ids:
            return candidate

    if "/" in raw:
        stripped = raw.split("/", 1)[1].strip()
        # Enterprise BYOK custom models expose ``owner/sub/model`` ids (two
        # slashes). A strip guess that still contains "/" cannot be a Copilot
        # id, so pass the input through untouched instead of corrupting it.
        if stripped and "/" not in stripped:
            return stripped
    return raw


def _github_reasoning_efforts_for_model_id(model_id: str) -> list[str]:
    raw = (model_id or "").strip().lower()
    if raw.startswith(("openai/o1", "openai/o3", "openai/o4", "o1", "o3", "o4")):
        return list(COPILOT_REASONING_EFFORTS_O_SERIES)
    normalized = normalize_copilot_model_id(model_id).lower()
    if is_astra_model(normalized):
        return list(CODEX_ASTRA_EFFORTS)
    if normalized.startswith("gpt-5"):
        return list(COPILOT_REASONING_EFFORTS_GPT5)
    return []


def _should_use_copilot_responses_api(model_id: str) -> bool:
    """opencode's ``shouldUseCopilotResponsesApi``: GPT-5+ uses the Responses API except
    ``gpt-5-mini``; non-GPT models (Claude, Gemini, ...) use Chat Completions."""
    match = re.match(r"^gpt-(\d+)", model_id)
    return bool(match) and int(match.group(1)) >= 5 and not model_id.startswith("gpt-5-mini")


def copilot_model_api_mode(
    model_id: Optional[str], *, catalog: Optional[list[dict[str, Any]]] = None,
    api_key: Optional[str] = None) -> str:
    """API mode for a Copilot model from the id pattern (opencode's approach). Copilot's Claude models
    go through its OpenAI-compatible chat endpoint, not the native Anthropic adapter: the catalog may
    advertise /v1/messages but the Copilot token/header scheme lives in the OpenAI client path."""
    if catalog is None and api_key:  # fetch once so normalize + endpoint check share it
        catalog = fetch_github_model_catalog(api_key=api_key)
    normalized = normalize_copilot_model_id(model_id, catalog=catalog, api_key=api_key)
    if normalized and _should_use_copilot_responses_api(normalized):
        return "codex_responses"
    return "chat_completions"


def azure_foundry_model_api_mode(model_name: Optional[str]) -> Optional[str]:
    """``"codex_responses"`` for families that only accept the Responses API on Azure Foundry (GPT-5.x
    incl. gpt-5-mini, codex, o1/o3/o4), else None. Any ``vendor/`` prefix is stripped first."""
    raw = str(model_name or "").strip().lower().rsplit("/", 1)[-1]
    return "codex_responses" if raw and raw.startswith(tuple(_AZURE_FOUNDRY_RESPONSES_PREFIXES)) else None


_OPENCODE_FAMILIES = ("opencode-go", "opencode-zen")


def opencode_provider_family(provider_id: Optional[str]) -> Optional[str]:
    """Resolve a provider id (canonical or prefixed) to its OpenCode family, or None.

    Returns ``"opencode-zen"`` or ``"opencode-go"`` for the built-in providers AND for custom providers
    whose name extends a family slug (e.g. ``opencode-go-bridge`` pointing at
    ``https://opencode.ai/zen/go/v1``, issue #85589). Matching is case-insensitive. Custom family providers
    need the same per-model api_mode routing and /v1 base-url normalization as the built-ins — this
    predicate is the single owner of that family-membership question; do not re-implement it inline.
    """
    raw = str(provider_id or "").strip().lower()
    if not raw:
        return None
    canonical = normalize_provider(provider_id)
    if canonical in _OPENCODE_FAMILIES:
        return canonical
    return next((f for f in _OPENCODE_FAMILIES if raw.startswith(f)), None)


def normalize_opencode_model_id(provider_id: Optional[str], model_id: Optional[str]) -> str:
    """Normalize OpenCode config IDs to the bare model slug used in API requests."""
    family = opencode_provider_family(provider_id)
    current = str(model_id or "").strip()
    if not current or family is None:
        return current
    for prefix in (f"{provider_id or family}/", f"{family}/"):
        if current.lower().startswith(prefix.lower()):
            return current[len(prefix):]
    return current


# Per-family (model-id prefix → api_mode) routing from OpenCode's published Zen/Go endpoint
# tables, checked in order. GPT/Codex/Grok and Muse Spark use /v1/responses (Muse Spark 503s on
# chat/completions); Claude (Zen), MiniMax (Go), Union Alpha, and Qwen use /v1/messages;
# everything else falls through to /v1/chat/completions.
_OPENCODE_API_MODE_PREFIXES: dict[str, tuple[tuple[tuple[str, ...], str], ...]] = {
    "opencode-go": (
        (("gpt-", "grok-", "muse-spark"), "codex_responses"),
        (("minimax-", "qwen", "union-alpha"), "anthropic_messages")),
    "opencode-zen": (
        (("claude-", "union-alpha"), "anthropic_messages"), (("gpt-", "grok-", "muse-spark"), "codex_responses"),
        (("qwen",), "anthropic_messages"))}


def opencode_model_api_mode(provider_id: Optional[str], model_id: Optional[str]) -> str:
    """Determine the API mode for an OpenCode Zen / Go model (see ``_OPENCODE_API_MODE_PREFIXES``)."""
    family = opencode_provider_family(provider_id)
    normalized = normalize_opencode_model_id(provider_id, model_id).lower()
    if normalized:
        for prefixes, mode in _OPENCODE_API_MODE_PREFIXES.get(family or "", ()):
            if normalized.startswith(prefixes):
                return mode
    return "chat_completions"


# Relay path per OpenCode family on opencode.ai hosts.
_OPENCODE_FAMILY_PATHS = {"opencode-zen": "/zen", "opencode-go": "/zen/go"}


def normalize_opencode_base_url(
    provider_id: Optional[str], api_mode: Optional[str], base_url: Optional[str]) -> str:
    """Normalize an OpenCode Zen / Go base URL for the API mode. Must be SYMMETRIC: the anthropic-
    stripped URL gets persisted to ``model.base_url`` after switching into an anthropic-routed model,
    and chat/codex modes heal it by re-adding ``/v1`` — but only on opencode.ai hosts, so custom
    ``OPENCODE_*_BASE_URL`` proxies are left alone. On those hosts the relay path segment follows
    the resolved family too (``/zen`` vs ``/zen/go``): the two relays serve different model sets,
    so a ``model.base_url`` carried over from the other family 401s ("Model ... is not supported").
    The family heal applies to the BUILT-IN providers only: a custom provider merely named after a
    family (``opencode-go-bridge``) declared its relay path explicitly in ``providers:`` and keeps it.
    Only the path is edited, so a port, userinfo, query or fragment round-trips untouched."""
    url = str(base_url or "").strip().rstrip("/")
    family = opencode_provider_family(provider_id)
    if not url or family is None:
        return url
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    official = host == "opencode.ai" or host.endswith(".opencode.ai")
    path = parsed.path.rstrip("/")
    if official and normalize_provider(provider_id) in _OPENCODE_FAMILIES and re.fullmatch(r"/zen(/go)?(/v1)?", path):
        path = _OPENCODE_FAMILY_PATHS[family] + ("/v1" if path.endswith("/v1") else "")
    if api_mode == "anthropic_messages":
        path = re.sub(r"/v1$", "", path)
    elif official and not path.endswith("/v1"):
        path += "/v1"
    return urllib.parse.urlunparse(parsed._replace(path=path))


def github_model_reasoning_efforts(
    model_id: Optional[str], *, catalog: Optional[list[dict[str, Any]]] = None,
    api_key: Optional[str] = None) -> list[str]:
    """Return supported reasoning-effort levels for a Copilot-visible model."""
    normalized = normalize_copilot_model_id(model_id, catalog=catalog, api_key=api_key)
    if not normalized:
        return []

    if catalog is None and api_key:
        catalog = fetch_github_model_catalog(api_key=api_key)
    catalog_entry = next((item for item in catalog if item.get("id") == normalized), None) if catalog else None
    if catalog_entry is not None:
        capabilities = catalog_entry.get("capabilities")
        if isinstance(capabilities, dict):
            # Structured catalog: the advertised list is authoritative (empty when absent).
            supports = capabilities.get("supports")
            efforts = supports.get("reasoning_effort") if isinstance(supports, dict) else None
            if not isinstance(efforts, list):
                return []
            return list(dict.fromkeys(e for effort in efforts if (e := str(effort).strip().lower())))
        # Legacy list-shaped capabilities: only a "reasoning" tag unlocks the pattern defaults.
        if "reasoning" not in {str(c).strip().lower() for c in catalog_entry.get("capabilities", [])}:
            return []
    return _github_reasoning_efforts_for_model_id(str(model_id or normalized))


# Negative cache: monotonic timestamp of the last timed-out probe, keyed
# by ``host:port`` so both URL candidates (``/v1`` + root) share one entry.
# Without this, an unreachable endpoint (TCP blackhole — SYN draws no reply,
# so every attempt burns its full connect timeout) makes every picker open /
# chat turn re-pay the timeout per candidate, and the sequential stalls stack
# past 10s while the Desktop sits on a spinner with no error (#81123). Short
# TTL collapses the burst but still picks up recovery without a restart.
# Mirrors _deepinfra_catalog_neg_cache.
_probe_neg_cache: dict[str, float] = {}
_PROBE_NEG_TTL = 60.0  # seconds


def _probe_neg_key(base_url: str) -> Optional[str]:
    """``host:port`` for *base_url* (both URL candidates share one entry), or None without a host."""
    from utils import base_url_origin

    _, host, port = base_url_origin(base_url)
    return f"{host}:{port}" if host else None


def _probe_result(
    models, probed_url, resolved_base_url, suggested_base_url=None, used_fallback=False
) -> dict[str, Any]:
    return {
        "models": models,
        "probed_url": probed_url,
        "resolved_base_url": resolved_base_url,
        "suggested_base_url": suggested_base_url,
        "used_fallback": used_fallback}


def probe_api_models(
    api_key: Optional[str], base_url: Optional[str], timeout: float = 5.0,
    api_mode: Optional[str] = None, request_headers: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Probe a ``/models`` endpoint with light URL heuristics (``base`` then ``base±/v1``).
    ``anthropic_messages`` mode sends ``x-api-key`` + ``anthropic-version`` instead of a bearer; the
    ``data[].id`` response shape is identical. ``models`` is None when no candidate answered."""
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        return _probe_result(None, None, "")
    if _is_github_models_base_url(normalized):
        models = _fetch_github_models(api_key=api_key, timeout=timeout)
        return _probe_result(models, COPILOT_MODELS_URL, COPILOT_BASE_URL)

    alternate_base = normalized[:-3].rstrip("/") if normalized.endswith("/v1") else normalized + "/v1"
    candidates: list[tuple[str, bool]] = [(normalized, False)]
    if alternate_base and alternate_base != normalized:
        candidates.append((alternate_base, True))

    tried: list[str] = []
    _neg_key = _probe_neg_key(normalized)
    if _neg_key is not None:
        _neg_seen = _probe_neg_cache.get(_neg_key)
        if _neg_seen is not None and (time.monotonic() - _neg_seen) < _PROBE_NEG_TTL:
            return _probe_result(
                None, normalized.rstrip("/") + "/models", normalized,
                alternate_base if alternate_base != normalized else None)
    headers: dict[str, str] = {"User-Agent": _HERMES_USER_AGENT}
    if urllib.parse.urlparse(normalized).hostname == "generativelanguage.googleapis.com":
        headers["X-Goog-Api-Client"] = f"hermes-agent/{get_version_info().base_version}"
    if api_key and api_mode == "anthropic_messages":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if normalized.startswith(COPILOT_BASE_URL):
        headers.update(copilot_default_headers())
    if isinstance(request_headers, dict):
        # Per-provider custom headers can contain secrets: merge last so endpoint config wins; never log.
        from hermes_cli.config import normalize_extra_headers

        headers.update(normalize_extra_headers(request_headers))

    # Only thread ssl_context when a per-provider TLS override applies; public endpoints keep the
    # original 2-arg call so existing call-seam mocks stay valid.
    _open_kwargs: dict[str, Any] = {}
    _ssl_context = _custom_provider_ssl_context(normalized)
    if _ssl_context is not None:
        _open_kwargs["ssl_context"] = _ssl_context
    all_timed_out = True
    for candidate_base, is_fallback in candidates:
        url = candidate_base.rstrip("/") + "/models"
        tried.append(url)
        try:
            data = _get_json(url, timeout=timeout, headers=headers, **_open_kwargs)
        except Exception as exc:
            # TLS, authentication and parsing failures must not hide corrected settings.
            cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            all_timed_out = all_timed_out and isinstance(cause, TimeoutError)
            continue
        if _neg_key is not None:
            _probe_neg_cache.pop(_neg_key, None)
        from hermes_cli.chat_catalog import note_catalog_item

        probed = []
        for item in data.get("data", []):
            if isinstance(item, dict) and note_catalog_item(item):
                continue
            probed.append(item.get("id", ""))
        return _probe_result(
            probed, url, candidate_base.rstrip("/"),
            alternate_base if alternate_base != candidate_base else normalized, is_fallback)

    if _neg_key is not None and all_timed_out:
        _probe_neg_cache[_neg_key] = time.monotonic()
    return _probe_result(
        None, tried[0] if tried else normalized.rstrip("/") + "/models", normalized,
        alternate_base if alternate_base != normalized else None)


# Legacy id-regex filter for items with no surface tag; unreachable (deletable) once every catalog
# entry carries an explicit ``chat``/``embed``/``image-gen``/``tts``/``stt`` tag.
_DEEPINFRA_EXCLUDE_RE = re.compile(
    r"(?i)(embed|rerank|whisper|stable-diffusion|flux|sdxl|"
    r"tts|bark|speech|image-gen|clip|vit-|dpt-)")

# Surface tags say *what kind of model* this is. Absent all of them, the tags array only carries
# capability tags (``reasoning``, ``vision``, …) and the chat surface falls back to id-regex inference.
_DEEPINFRA_SURFACE_TAGS: frozenset[str] = frozenset({
    "chat", "embed", "image-gen", "tts", "stt", "video-gen"})

_DEEPINFRA_DEFAULT_BASE_URL = "https://api.deepinfra.com/v1/openai"
_DEEPINFRA_MODELS_QUERY = "filter=true&sort_by=hermes"

# Full tagged catalog keyed by base URL; every surface filter reads it so one round-trip serves all.
_deepinfra_catalog_cache: dict[str, list[dict]] = {}

# Negative cache (monotonic time of the last failed fetch per base URL) so an unreachable catalog
# doesn't make every surface helper eat the full timeout in turn. Short TTL so connectivity recovers.
_deepinfra_catalog_neg_cache: dict[str, float] = {}
_DEEPINFRA_CATALOG_NEG_TTL = 60.0  # seconds


def _deepinfra_env(key: str) -> str:
    """Profile-scoped ``.env``/environ read: under a multiplexed turn the launch env is not this profile's."""
    from hermes_cli.config import get_env_value_prefer_dotenv
    return (get_env_value_prefer_dotenv(key) or "").strip()


def _deepinfra_catalog_url(
    base_url: Optional[str] = None,
) -> tuple[str, str]:
    """Return ``(cache_key, full_url)`` for the DeepInfra catalog endpoint."""
    base = str(base_url or "").strip() or os.getenv("DEEPINFRA_BASE_URL", "").strip() or _DEEPINFRA_DEFAULT_BASE_URL
    cache_key = base.rstrip("/")
    return cache_key, f"{cache_key}/models?{_DEEPINFRA_MODELS_QUERY}"


def _fetch_deepinfra_catalog(
    *,
    timeout: float = 5.0,
    force_refresh: bool = False,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Optional[list[dict]]:
    """Fetch the raw DeepInfra catalog list with module-level caching.

    The endpoint serves chat + embed + image-gen + tts + stt models in one
    response. Authentication is optional but Bearer-attached when available
    so user-scoped catalogs (private fine-tunes etc.) are visible.
    """
    cache_key, url = _deepinfra_catalog_url(base_url)
    if not force_refresh:
        if cache_key in _deepinfra_catalog_cache:
            return _deepinfra_catalog_cache[cache_key]
        last_fail = _deepinfra_catalog_neg_cache.get(cache_key)
        if last_fail is not None and (time.monotonic() - last_fail) < _DEEPINFRA_CATALOG_NEG_TTL:
            return None

    headers: dict[str, str] = {"User-Agent": _HERMES_USER_AGENT}
    resolved_api_key = (
        str(api_key).strip()
        if api_key is not None
        else os.getenv("DEEPINFRA_API_KEY", "").strip()
    )
    if resolved_api_key:
        headers["Authorization"] = f"Bearer {resolved_api_key}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with _urlopen_model_catalog_request(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except Exception:
        _deepinfra_catalog_neg_cache[cache_key] = time.monotonic()
        return None

    data = payload.get("data")
    if not isinstance(data, list):
        _deepinfra_catalog_neg_cache[cache_key] = time.monotonic()
        return None

    _deepinfra_catalog_cache[cache_key] = data
    _deepinfra_catalog_neg_cache.pop(cache_key, None)
    return data


def _fetch_deepinfra_models_by_tag(
    tag: str,
    *,
    timeout: float = 5.0,
    force_refresh: bool = False,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Optional[list[dict]]:
    """Return DeepInfra models whose ``metadata.tags`` includes *tag*.

    Each returned item is ``{"id": str, "metadata": dict}`` so callers can
    inspect context length, pricing, default dimensions (image-gen),
    pricing units (tts ``input_characters``, stt ``input_seconds``), etc.

    For the chat surface, items without any ``tags`` field fall through
    to the legacy name-regex exclusion so this keeps working while the
    tag rollout (mid-2026) is still in flight.

    Returns ``None`` on network failure.
    """
    data = _fetch_deepinfra_catalog(
        timeout=timeout,
        force_refresh=force_refresh,
        api_key=api_key,
        base_url=base_url,
    )
    if data is None:
        return None

    matched: list[dict] = []
    for item in data:
        mid = item.get("id")
        if not mid:
            continue
        # ``metadata is None`` means DeepInfra returns a stub without
        # pricing/context — typically a model that's listed but not
        # served. Skip those for every surface.
        raw_metadata = item.get("metadata")
        if raw_metadata is None:
            continue
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        raw_tags = metadata.get("tags")
        tags = raw_tags if isinstance(raw_tags, list) else []
        has_surface_tag = any(t in _DEEPINFRA_SURFACE_TAGS for t in tags)

        if has_surface_tag:
            if tag in tags:
                matched.append({"id": mid, "metadata": metadata})
            continue
        # Surface-tag rollout incomplete — fall back to id-regex inference.
        # Only meaningful for the chat surface; embed/image-gen/tts/stt
        # cannot be safely inferred from an id alone.
        if tag == "chat" and not _DEEPINFRA_EXCLUDE_RE.search(mid):
            matched.append({"id": mid, "metadata": metadata})

    return matched


def _fetch_deepinfra_models(
    timeout: float = 5.0,
    *,
    force_refresh: bool = False,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Optional[list[str]]:
    """Return DeepInfra chat-model ids (tag-aware, regex fallback).

    Thin wrapper over :func:`_fetch_deepinfra_models_by_tag` so historical
    callers in :func:`provider_model_ids` keep their string-list contract.
    Returns ``None`` on network failure, an empty list if the catalog
    contains no chat-tagged ids (which would itself be surprising).
    """
    items = _fetch_deepinfra_models_by_tag(
        "chat",
        timeout=timeout,
        force_refresh=force_refresh,
        api_key=api_key,
        base_url=base_url,
    )
    if items is None:
        return None
    return [item["id"] for item in items] or None


def deepinfra_model_ids(tag: str, *, force_refresh: bool = False) -> list[str]:
    """Return DeepInfra model ids carrying surface *tag* (``[]`` on failure)."""
    items = _fetch_deepinfra_models_by_tag(tag, force_refresh=force_refresh)
    return [item["id"] for item in items] if items else []


def deepinfra_base_url(section: Optional[dict] = None) -> str:
    """DeepInfra base URL: config-section ``base_url`` → ``DEEPINFRA_BASE_URL`` env → default; stripped."""
    candidate = section.get("base_url") if isinstance(section, dict) else None
    value = candidate or _deepinfra_env("DEEPINFRA_BASE_URL") or _DEEPINFRA_DEFAULT_BASE_URL
    return str(value).strip().rstrip("/")


def _fetch_ai_gateway_models(
    timeout: float = 5.0,
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Optional[list[str]]:
    """Fetch available language models with tool-use from AI Gateway."""
    resolved_api_key = (
        str(api_key).strip()
        if api_key is not None
        else os.getenv("AI_GATEWAY_API_KEY", "").strip()
    )
    if not resolved_api_key:
        return None
    resolved_base_url = str(base_url).strip() if base_url is not None else os.getenv("AI_GATEWAY_BASE_URL", "").strip()
    if not resolved_base_url:
        from hermes_constants import AI_GATEWAY_BASE_URL
        resolved_base_url = AI_GATEWAY_BASE_URL

    url = resolved_base_url.rstrip("/") + "/models"
    headers: dict[str, str] = {
        "Authorization": f"Bearer {resolved_api_key}",
        "User-Agent": _HERMES_USER_AGENT,
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return [
                m["id"]
                for m in data.get("data", [])
                if m.get("id")
                and m.get("type") == "language"
                and "tool-use" in (m.get("tags") or [])
            ]
    except Exception:
        return None


def fetch_api_models(
    api_key: Optional[str], base_url: Optional[str], timeout: float = 5.0,
    api_mode: Optional[str] = None, headers: Optional[dict[str, str]] = None,
) -> Optional[list[str]]:
    """Fetch the list of available model IDs from the provider's ``/models`` endpoint."""
    result = probe_api_models(api_key, base_url, timeout=timeout, api_mode=api_mode, request_headers=headers)
    return result.get("models")


def _custom_endpoint_fingerprint(
    api_key: Any, api_mode: Optional[str], headers: Optional[dict[str, str]]) -> str:
    """Custom endpoints have no ``PROVIDER_REGISTRY`` slug, so hash exactly what callers pass to
    :func:`fetch_api_models`: a rotated ``api_key``, changed ``api_mode`` or edited ``extra_headers``
    each bust the cache entry. blake2b for the same CodeQL rationale as ``_credential_fingerprint``."""
    import hashlib

    from agent.command_token_source import CommandTokenSource
    identity = api_key.cache_identity if isinstance(api_key, CommandTokenSource) else api_key
    blob = "|".join((identity or "", api_mode or "", json.dumps(headers or {}, sort_keys=True)))
    return hashlib.blake2b(blob.encode("utf-8", errors="replace"), digest_size=8).hexdigest()


def _cache_entry_valid(
    entry: Any, fp: str, *, allow_empty: bool = False) -> "TypeGuard[dict[str, Any]]":
    """Well-formed cache row for fingerprint *fp*. Requires a numeric ``at`` so corrupt disk state
    degrades to a cache miss instead of raising; empty model lists are valid only when the caller
    opts into an authoritative empty catalog."""
    return (
        isinstance(entry, dict)
        and entry.get("fp") == fp
        and isinstance(entry.get("models"), list)
        and (allow_empty or bool(entry["models"]))
        and isinstance(entry.get("at"), (int, float))
        and not isinstance(entry.get("at"), bool))


def _disk_serve_tier(entry: Any, fp: str, now: float, *, is_ollama: bool,
                     ttl_seconds: int = _PROVIDER_MODELS_CACHE_TTL) -> Optional[str]:
    """How :func:`cached_provider_model_ids` serves *entry* without the network.

    ``"fresh"`` inside the row's TTL (a curated fallback row only for
    ``_PROVIDER_MODELS_FALLBACK_TTL``), ``"stale"`` for a non-empty, non-fallback row inside
    ``_PROVIDER_MODELS_STALE_SERVE_MAX`` (served while an SWR thread revalidates), else ``None``:
    the call would block on a live fetch. Empty native catalogs are authoritative only inside the
    short native TTL, never through the stale window."""
    if is_ollama:
        ttl_seconds = min(ttl_seconds, _OLLAMA_LOCAL_MODELS_CACHE_TTL)
    if not _cache_entry_valid(entry, fp, allow_empty=is_ollama):
        return None
    age = now - entry["at"]
    if age < (_PROVIDER_MODELS_FALLBACK_TTL if entry.get("fallback") else ttl_seconds):
        return "fresh"
    if entry["models"] and not entry.get("fallback") and age < _PROVIDER_MODELS_STALE_SERVE_MAX:
        return "stale"
    return None


def cached_fetch_api_models(
    api_key: Optional[str],
    base_url: Optional[str],
    *,
    timeout: float = 5.0,
    api_mode: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
    force_refresh: bool = False,
    cache_only: bool = False,
    require_verified: bool = False,
    ttl_seconds: int = _PROVIDER_MODELS_CACHE_TTL,
) -> Optional[list[str]]:
    """Disk-cached wrapper around :func:`fetch_api_models` for custom endpoints.

    Mirrors :func:`cached_provider_model_ids` — including its
    stale-while-revalidate tier — but keys ``provider_models_cache.json``
    off ``custom:<base_url>`` instead of a ``PROVIDER_REGISTRY`` slug, since
    custom endpoints (named ``custom_providers`` rows, bare
    ``provider: custom``, and per-endpoint-map entries) have none. Same
    stale-beats-nothing fallback policy: a live-fetch failure serves the
    last same-fingerprint result rather than an empty list. Returns whatever
    :func:`fetch_api_models` would (a list or ``None``); corrupt cache rows
    degrade to a live fetch instead of raising.

    ``cache_only`` serves a previously-discovered catalog without touching
    the network at all — no live fetch, no background revalidation — and
    returns ``None`` when nothing usable is cached. Callers that deliberately
    skip live probing for latency reasons (GUI picker opens, which must not
    block on a stopped local endpoint) use this so a warm catalog still
    reaches the picker instead of collapsing to the config-declared subset.
    """
    normalized_url = str(base_url or "").strip().rstrip("/").lower()
    if not normalized_url:
        if cache_only or require_verified:
            return None
        # No base_url means nothing to key the cache on — fall through to a
        # live call so callers keep getting fetch_api_models' own behavior.
        return fetch_api_models(
            api_key, base_url, timeout=timeout, api_mode=api_mode, headers=headers
        )

    cache_key = f"custom:{normalized_url}"
    fp = _custom_endpoint_fingerprint(api_key, api_mode, headers)
    cache = _load_provider_models_cache()
    entry = cache.get(cache_key)
    now = time.time()

    if cache_only:
        # Same trust window as the stale-while-revalidate tier below, minus
        # the revalidation: an entry this side of the bound is good enough to
        # render, and anything older is treated as a miss so the caller falls
        # back to its configured list rather than showing a stale catalog.
        if force_refresh or not _cache_entry_valid(entry, fp):
            return None
        if now - entry["at"] >= _PROVIDER_MODELS_STALE_SERVE_MAX:
            return None
        models = list(entry["models"])
        if require_verified:
            verified = entry.get("verified_models") if isinstance(entry, dict) else None
            return [model for model in models if isinstance(verified, list) and model in set(verified)]
        return models

    if not force_refresh and _cache_entry_valid(entry, fp):
        age = now - entry["at"]
        if age < ttl_seconds:
            models = list(entry["models"])
            if require_verified:
                verified = entry.get("verified_models") if isinstance(entry, dict) else None
                return [model for model in models if isinstance(verified, list) and model in set(verified)]
            return models
        if age < _PROVIDER_MODELS_STALE_SERVE_MAX:
            # Stale-while-revalidate: serve the expired entry immediately so
            # picker opens never block on a live /v1/models round-trip
            # (#72762's stall class, which a plain TTL would reintroduce an
            # hour into the session); refresh off-thread for the next open.
            def _refresh_custom():
                live = fetch_api_models(
                    api_key, base_url,
                    timeout=timeout, api_mode=api_mode, headers=headers,
                )
                if not live:
                    return None
                return {
                    "fp": fp,
                    "at": time.time(),
                    "models": list(live),
                    "verified_models": list(live),
                }

            _spawn_swr_refresh(cache_key, _refresh_custom)
            models = list(entry["models"])
            if require_verified:
                verified = entry.get("verified_models") if isinstance(entry, dict) else None
                return [model for model in models if isinstance(verified, list) and model in set(verified)]
            return models

    live = fetch_api_models(
        api_key, base_url, timeout=timeout, api_mode=api_mode, headers=headers
    )
    if live:
        cache[cache_key] = {
            "fp": fp,
            "at": now,
            "models": list(live),
            "verified_models": list(live),
        }
        _save_provider_models_cache(cache)
        return list(live)

    # Live fetch returned nothing (offline endpoint, timeout, auth hiccup).
    # A stale same-fingerprint entry beats an empty result for ordinary picker
    # callers, but admission must reject rather than reuse it.
    if require_verified:
        return []
    if _cache_entry_valid(entry, fp):
        return list(entry["models"])
    return live


class ProviderModelCatalog(list[str]):
    """List-compatible model catalog carrying minimal live-verification provenance."""

    def __init__(self, models=(), *, verified_models=()):
        super().__init__(models or ())
        self.verified_models = frozenset(
            str(model).strip() for model in (verified_models or ()) if str(model).strip()
        )


def _catalog_result(models, *, verified_models=()) -> ProviderModelCatalog:
    """Return a list-compatible catalog with explicit verified model IDs."""
    if isinstance(models, ProviderModelCatalog):
        return models
    return ProviderModelCatalog(models or (), verified_models=verified_models)


def _save_ollama_cloud_cache(
    models: list[str],
    *,
    verified_models: Optional[list[str]] = None,
) -> None:
    """Persist the merged Ollama Cloud model list to disk."""
    try:
        from utils import atomic_json_write
        cache_path = _ollama_cloud_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"models": models, "cached_at": time.time()}
        if verified_models is not None:
            payload["verified_models"] = list(verified_models)
        atomic_json_write(cache_path, payload, indent=None)
    except Exception:
        pass


def fetch_ollama_cloud_models(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    *,
    force_refresh: bool = False,
    cache_only: bool = False,
    return_catalog: bool = False,
) -> list[str] | ProviderModelCatalog:
    """Fetch Ollama Cloud models by merging live API + models.dev, with disk cache.

    Resolution order:
      1. Disk cache (if fresh, < 1 hour, and not force_refresh)
      2. Live ``/v1/models`` endpoint (primary — freshest source)
      3. models.dev registry (secondary — fills gaps for unlisted models)
      4. Merge: live models first, then models.dev additions (deduped)

    Returns a list of model IDs (never None — empty list on total failure).
    ``cache_only`` never probes the live API or writes the disk cache;
    stale cache takes precedence over a models.dev-only fallback.
    """
    # 1. Check disk cache
    if not force_refresh:
        cached = _load_ollama_cloud_cache()
        if cached is not None:
            if return_catalog:
                return _catalog_result(
                    cached["models"],
                    verified_models=cached.get("verified_models", ()),
                )
            return cached["models"]

    # 2. Live API probe
    if not api_key:
        api_key = os.getenv("OLLAMA_API_KEY", "")
    if not base_url:
        base_url = os.getenv("OLLAMA_BASE_URL", "") or "https://ollama.com/v1"

    live_models: list[str] = []
    if api_key and not cache_only:
        result = fetch_api_models(api_key, base_url, timeout=8.0)
        if result:
            live_models = result

    # 3. models.dev registry
    mdev_models: list[str] = []
    try:
        from agent.models_dev import list_agentic_models
        mdev_models = list_agentic_models("ollama-cloud")
    except Exception:
        pass

    # 4. Merge: live first, then models.dev additions (deduped, order-preserving)
    merged: list[str] = []
    if live_models or mdev_models:
        seen: set[str] = set()
        for m in live_models:
            if m and m not in seen:
                seen.add(m)
                merged.append(m)
        for m in mdev_models:
            normalized = _strip_ollama_cloud_suffix(m)
            if normalized and normalized not in seen:
                seen.add(normalized)
                merged.append(normalized)
        if live_models:
            _save_ollama_cloud_cache(merged, verified_models=live_models)
            if return_catalog:
                return _catalog_result(merged, verified_models=live_models)
            return merged

    # Total failure — return stale cache if available (ignore TTL)
    stale = _load_ollama_cloud_cache(ignore_ttl=True)
    if stale is not None:
        if return_catalog:
            return _catalog_result(
                stale["models"],
                verified_models=stale.get("verified_models", ()),
            )
        return stale["models"]

    return _catalog_result(merged) if return_catalog else merged
