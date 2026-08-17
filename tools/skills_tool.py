#!/usr/bin/env python3
"""Skills Tool — list and view skill documents (progressive disclosure). A skill is a directory
holding SKILL.md (YAML frontmatter + instructions) plus optional references/, templates/, assets/,
scripts/. `skills_list` returns name/description only; `skill_view` returns full content and
linked files. Sibling modules (skills_tool_setup / _plugin / _dedup) re-export here."""

import hashlib
import json
import logging
import os
import time
from contextlib import suppress
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error
from hermes_cli.config import cfg_get
from agent.skill_utils import (
    EXCLUDED_SKILL_DIRS as _EXCLUDED_SKILL_DIRS, is_skill_support_path as _is_skill_support_path)
from tools.skills_tool_setup import (  # noqa: F401
    SkillReadinessStatus, _build_setup_note, _capture_required_environment_variables,
    _get_required_environment_variables, _is_env_var_persisted, _is_remote_env_backend)
from tools.skills_tool_plugin import (  # noqa: F401
    MAX_DESCRIPTION_LENGTH, MAX_NAME_LENGTH, _INJECTION_PATTERNS, _fail, _json,
    _mark_background_review_read, _preprocess_skill, _read_skill_text, _safe_frontmatter,
    _serve_plugin_skill, _serve_skill_file, _truncate_description)
from tools.skills_tool_dedup import (  # noqa: F401
    _check_skill_view_dedup, _record_skill_view, reset_skill_view_dedup)
from tools.skill_provenance import is_background_review

logger = logging.getLogger(__name__)

# Per-session discovery cache: {cache_key: (signature, timestamp, skills_list)}. Signature =
# per-dir max mtime of the dir and its immediate children (add/remove inside a category does
# NOT bump the root mtime) + the disabled set (config-only change, no mtime) + platform; the
# TTL bounds staleness from in-place SKILL.md edits, which no directory signature can see.
_SKILLS_CACHE: dict = {}
_SKILLS_CACHE_TTL_SECONDS = 30.0


def _skills_scan_signature(dirs_to_scan, disabled) -> tuple:
    """O(#dirs + #categories) stat-based change signature; platform is read via
    ``agent.skill_utils.sys`` so test patches are honored."""
    from agent import skill_utils as _skill_utils
    platform = getattr(getattr(_skill_utils, "sys", None), "platform", "")
    sig = []
    for d in dirs_to_scan:
        try:
            m = d.stat().st_mtime
        except OSError:
            continue
        with suppress(OSError), os.scandir(d) as it:
            for entry in it:
                with suppress(OSError):
                    if entry.is_dir(follow_symlinks=False):
                        m = max(m, entry.stat(follow_symlinks=False).st_mtime)
        sig.append((str(d), m))
    return (tuple(sig), frozenset(disabled), platform)


HERMES_HOME = get_hermes_home()  # all skills live in ~/.hermes/skills/ (seeded from bundled)
SKILLS_DIR = HERMES_HOME / "skills"
_SKILLS_DIR_AT_IMPORT = SKILLS_DIR


def _skills_dir() -> Path:
    """Active profile's skills dir at call time: the patched ``SKILLS_DIR`` when a patcher changed
    it, else live profile-scoped HERMES_HOME (long-lived runtimes may import before profile set)."""
    configured = Path(SKILLS_DIR)
    return configured if configured != _SKILLS_DIR_AT_IMPORT else get_hermes_home() / "skills"


_secret_capture_callback = None
_LOOKUP_HINT = "Use a skill name or relative path within the skills directory."


def _skill_lookup_path_error(name: str) -> Optional[str]:
    """Error if lookup *name* could escape the search roots it is joined onto. Windows drive
    paths are rejected too: their ``:`` would be misread as a plugin namespace separator."""
    from tools.path_security import has_traversal_component
    if not isinstance(name, str):
        return "Skill name must be a string."
    win = PureWindowsPath(candidate := name.strip())
    if PurePosixPath(candidate).is_absolute() or win.is_absolute() or win.drive:
        return "Skill name must be a relative path within the skills directory."
    if has_traversal_component(candidate):
        return "Skill name cannot contain '..' path traversal components."
    return None


def load_env() -> Dict[str, str]:
    """Snapshot of HERMES_HOME/.env for the post-skill secret-capture diff (same tokenizer that
    installs the profile scope, so a captured value never differs from the served one)."""
    from agent.secret_scope import load_env_file

    return load_env_file(get_hermes_home() / ".env")


def set_secret_capture_callback(callback) -> None:
    global _secret_capture_callback
    _secret_capture_callback = callback


def _skill_utils_delegate(attr: str):
    """Lazy call-time delegate to ``agent.skill_utils.<attr>`` (re-export; patches honored)."""
    def _delegate(*args):
        from agent import skill_utils
        return getattr(skill_utils, attr)(*args)
    _delegate.__name__ = _delegate.__qualname__ = attr
    return _delegate


skill_matches_platform = _skill_utils_delegate("skill_matches_platform")
# Offer-time relevance gate (kanban/docker/s6), NOT hard compatibility; explicit loads bypass it.
skill_matches_environment = _skill_utils_delegate("skill_matches_environment")
skill_matches_apps = _skill_utils_delegate("skill_matches_apps")
_parse_frontmatter = _skill_utils_delegate("parse_frontmatter")
_get_disabled_skill_names = _skill_utils_delegate("get_disabled_skill_names")


def check_skills_requirements() -> bool:
    return True  # always available: the directory is created on first use


def _get_category_from_path(skill_path: Path) -> Optional[str]:
    """``~/.hermes/skills/mlops/axolotl/SKILL.md`` -> ``"mlops"``; active profile dir first
    (respects test monkeypatching), then skills.external_dirs."""
    dirs_to_check = [_skills_dir()]
    with suppress(Exception):
        from agent.skill_utils import get_external_skills_dirs
        dirs_to_check.extend(get_external_skills_dirs())
    for skills_dir in dirs_to_check:
        with suppress(ValueError):
            if len(parts := skill_path.relative_to(skills_dir).parts) >= 3:
                return parts[0]
    return None


def _parse_tags(tags_value) -> List[str]:
    """Tags from frontmatter: a parsed list, "[a, b]", or "a, b"."""
    if not tags_value:
        return []
    if isinstance(tags_value, list):
        return [str(t).strip() for t in tags_value if t]
    tags_value = str(tags_value).strip()
    if tags_value.startswith("[") and tags_value.endswith("]"):
        tags_value = tags_value[1:-1]
    return [t.strip().strip("\"'") for t in tags_value.split(",") if t.strip()]


def _is_skill_disabled(name: str, platform: str = None) -> bool:
    """Disabled in config? Platform precedence: explicit arg, ``HERMES_PLATFORM``, session
    ``HERMES_SESSION_PLATFORM``. A globally-disabled skill stays disabled on every platform
    (keep in sync with agent.skill_utils.get_disabled_skill_names)."""
    try:
        from hermes_cli.config import load_config
        skills_cfg = load_config().get("skills", {})
        resolved_platform = platform or os.getenv("HERMES_PLATFORM")
        if not resolved_platform:
            with suppress(Exception):
                from gateway.session_context import get_session_env
                resolved_platform = get_session_env("HERMES_SESSION_PLATFORM") or ""
        platform_disabled = None
        if resolved_platform:
            platform_disabled = cfg_get(skills_cfg, "platform_disabled", resolved_platform)
        in_platform = platform_disabled is not None and name in platform_disabled
        return in_platform or name in skills_cfg.get("disabled", [])
    except Exception:
        return False


def _skill_search_dirs() -> Tuple[list, list, Path]:
    """(project_dirs, all_dirs, active_skills_dir); trusted project-local dirs come FIRST so
    first-wins dedup / the collision resolver prefer them."""
    from agent.skill_utils import get_external_skills_dirs, get_project_skills_dirs
    project_dirs = list(get_project_skills_dirs())
    active_skills_dir = _skills_dir()
    all_dirs = project_dirs + ([active_skills_dir] if active_skills_dir.exists() else [])
    all_dirs += get_external_skills_dirs()
    return project_dirs, all_dirs, active_skills_dir


_SKILLS_CACHE_KEY_DISABLED = "with_disabled"
_SKILLS_CACHE_KEY_FILTERED = "filtered"


def _find_all_skills(
    *,
    skip_disabled: bool = False,
    search_dirs: List[Path] | None = None,
    task_id: str | None = None,
) -> List[Dict[str, Any]]:
    """Recursively find all skills in ~/.hermes/skills/ and external dirs.

    Args:
        skip_disabled: If True, return ALL skills regardless of disabled
            state (used by ``hermes skills`` config UI). Default False
            filters out disabled skills.

    Returns:
        List of skill metadata dicts (name, description, category).

    Results are cached per-session; the cache is invalidated when the scan
    signature changes (dir/category mtimes or the disabled-set) and expires
    after a short TTL to bound staleness from in-place SKILL.md edits.
    """
    from agent.skill_utils import (
        get_external_skills_dirs,
        get_project_skills_dirs,
        iter_project_skill_files,
        iter_skill_index_files,
    )

    cache_key: Any = (
        (_SKILLS_CACHE_KEY_DISABLED if skip_disabled else _SKILLS_CACHE_KEY_FILTERED)
        if search_dirs is None
        else (
            "protected",
            tuple(str(path) for path in search_dirs),
            task_id,
            bool(skip_disabled),
        )
    )

    # Load disabled set once (not per-skill). Part of the cache signature:
    # disabling a skill is a config change with no filesystem mtime bump.
    disabled = (
        set()
        if skip_disabled or (search_dirs is not None and not task_id)
        else _get_disabled_skill_names()
    )

    # Collect directories to scan — same resolution as the scan loop below
    # (_skills_dir() resolves the LIVE profile HERMES_HOME; the module-level
    # SKILLS_DIR can be stale in long-lived runtimes). Trusted project-local
    # dirs come FIRST: first-wins dedup below gives them precedence over
    # same-named local/external skills.
    project_dirs = list(get_project_skills_dirs())
    if search_dirs is None:
        dirs_to_scan: list = list(project_dirs)
        active_skills_dir = _skills_dir()
        if active_skills_dir.exists():
            dirs_to_scan.append(active_skills_dir)
        dirs_to_scan.extend(get_external_skills_dirs())
    else:
        dirs_to_scan = list(search_dirs)
    if task_id:
        protected_dirs = _protected_skill_search_dirs(task_id, dirs_to_scan)
        if protected_dirs is None:
            return []
        dirs_to_scan = protected_dirs

    signature = _skills_scan_signature(dirs_to_scan, disabled)
    now = time.monotonic()

    cached = _SKILLS_CACHE.get(cache_key)
    if (
        cached is not None
        and cached[0] == signature
        and (now - cached[1]) < _SKILLS_CACHE_TTL_SECONDS
    ):
        # Per-call shallow copies: callers mutate the returned dicts
        # (e.g. web_server annotates s["enabled"]/s["usage"]) — handing
        # out the cached objects would poison the cache for everyone else.
        return [dict(s) for s in cached[2]]

    skills = []
    seen_names: set = set()

    # Scan project dirs first, then local, then external (first-wins) —
    # dirs_to_scan already resolved above for the signature. Project dirs
    # iterate through the quarantine chokepoint (scan-time injection gate).
    for scan_dir in dirs_to_scan:
        _is_project = scan_dir in project_dirs
        _iter = (
            iter_project_skill_files(scan_dir)
            if _is_project
            else iter_skill_index_files(scan_dir, "SKILL.md")
        )
        for skill_md in _iter:
            if any(part in _EXCLUDED_SKILL_DIRS for part in skill_md.parts):
                continue
            if task_id and _protected_skill_path_allowed(task_id, skill_md) is not True:
                continue

            skill_dir = skill_md.parent

            try:
                content = skill_md.read_text(encoding="utf-8-sig", errors="replace")[:4000]
                frontmatter, body = _parse_frontmatter(content)

                if not skill_matches_platform(frontmatter):
                    continue

                if not skill_matches_environment(frontmatter):
                    continue

                name = frontmatter.get("name", skill_dir.name)[:MAX_NAME_LENGTH]
                if name in seen_names:
                    continue
                if name in disabled:
                    continue

                description = frontmatter.get("description", "")
                if not description:
                    for line in body.strip().split("\n"):
                        line = line.strip()
                        if line and not line.startswith("#"):
                            description = line
                            break

                if len(description) > MAX_DESCRIPTION_LENGTH:
                    description = description[:MAX_DESCRIPTION_LENGTH - 3] + "..."

                category = _get_category_from_path(skill_md)

                seen_names.add(name)
                from agent.skill_utils import extract_skill_conditions

                skills.append({
                    "name": name,
                    "description": description,
                    "category": category,
                    "conditions": extract_skill_conditions(frontmatter),
                })

            except (UnicodeDecodeError, PermissionError) as e:
                logger.debug("Failed to read skill file %s: %s", skill_md, e)
                continue
            except Exception as e:
                logger.debug(
                    "Skipping skill at %s: failed to parse: %s", skill_md, e, exc_info=True
                )
                continue

    if task_id:
        viewable_skills = []
        for skill in skills:
            try:
                probe = json.loads(
                    skill_view(
                        skill["name"],
                        task_id=task_id,
                        preprocess=False,
                        _viewability_probe=True,
                    )
                )
            except Exception:
                probe = {"success": False}
            if probe.get("success"):
                viewable_skills.append(skill)
        skills = viewable_skills

    # Store in cache keyed by the scan signature computed BEFORE the scan
    # (a write racing the scan changes the signature, so the next call
    # re-scans rather than serving the torn result past the TTL). Same
    # shallow-copy contract as the hit path — the caller may mutate.
    _SKILLS_CACHE[cache_key] = (signature, now, skills)
    return [dict(s) for s in skills]


def _sort_skills(skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep every skill listing path ordered the same way."""
    return sorted(skills, key=lambda s: (s.get("category") or "", s["name"]))


def skills_list(category: str = None, task_id: str = None) -> str:
    """
    List all available skills (progressive disclosure tier 1 - minimal metadata).

    Returns only name + description to minimize token usage. Use skill_view() to
    load full content, tags, related files, etc.

    Args:
        category: Optional category filter (e.g., "mlops")
        task_id: Optional task identifier used to probe the active backend

    Returns:
        JSON string with minimal skill info: name, description, category
    """
    try:
        from agent.skill_utils import get_external_skills_dirs

        active_skills_dir = _skills_dir()
        candidate_dirs = []
        if active_skills_dir.exists():
            candidate_dirs.append(active_skills_dir)
        candidate_dirs.extend(get_external_skills_dirs())
        protected_dirs = _protected_skill_search_dirs(task_id, candidate_dirs)
        if protected_dirs is None and not active_skills_dir.exists():
            active_skills_dir.mkdir(parents=True, exist_ok=True)

        # Use the same effective catalog as protected prompt construction.
        all_skills = _effective_skills(
            task_id=task_id,
            candidate_dirs=candidate_dirs,
        )
        if not task_id:
            from hermes_cli.plugins import discover_plugins, get_plugin_manager

            discover_plugins()
            seen_names = {skill["name"] for skill in all_skills}
            all_skills.extend(
                skill
                for skill in get_plugin_manager().list_plugin_skill_metadata()
                if skill["name"] not in seen_names
            )

        if not all_skills:
            return json.dumps(
                {
                    "success": True,
                    "skills": [],
                    "categories": [],
                    "message": "No skills found in skills/ directory.",
                },
                ensure_ascii=False,
            )

        # Filter by category if specified
        if category:
            all_skills = [s for s in all_skills if s.get("category") == category]

        all_skills = [
            {key: value for key, value in skill.items() if key != "conditions"}
            for skill in all_skills
        ]

        # Sort by category then name
        all_skills = _sort_skills(all_skills)

        # Extract unique categories
        categories = sorted(
            {s.get("category") for s in all_skills if s.get("category")}
        )

        return json.dumps(
            {
                "success": True,
                "skills": all_skills,
                "categories": categories,
                "count": len(all_skills),
                "hint": "Use skill_view(name) to see full content, tags, and linked files",
            },
            ensure_ascii=False,
        )

    except Exception as e:
        return tool_error(str(e), success=False)


def _resolve_plugin_skill(name, file_path, task_id, preprocess):
    """``plugin:skill`` dispatch: ``(result_json, None)`` when answered, else ``(None,
    local_category_name)`` to fall through to the flat-tree scan — categorized local skills also use
    ``category:skill`` in config/gateway prompts, so the on-disk ``category/skill`` form returns."""
    from agent.skill_utils import is_valid_namespace, parse_qualified_name
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    namespace, bare = parse_qualified_name(name)
    if not is_valid_namespace(namespace):
        return _fail(f"Invalid namespace '{namespace}' in '{name}'. Namespaces must match [a-zA-Z0-9_-]+."), None
    discover_plugins()  # idempotent
    pm = get_plugin_manager()
    active_memory_provider = None
    try:
        from plugins.memory import _get_active_memory_provider, _prune_inactive_memory_provider_skills
        active_memory_provider = _get_active_memory_provider()
        _prune_inactive_memory_provider_skills(active_memory_provider)
    except Exception as exc:
        logger.debug("Failed pruning inactive memory-provider skills: %s", exc)
    plugin_skill_md = pm.find_plugin_skill(name)
    # Memory providers load through plugins.memory, not the general PluginManager: load the
    # namespaced provider once so its collector can forward its skills into the registry.
    if plugin_skill_md is None and namespace == active_memory_provider:
        try:
            from plugins.memory import load_memory_provider
            load_memory_provider(namespace)
            plugin_skill_md = pm.find_plugin_skill(name)
        except Exception as exc:
            logger.debug("Failed lazy memory-provider skill load for %s: %s", namespace, exc)
    if plugin_skill_md is not None and not plugin_skill_md.exists():
        pm.remove_plugin_skill(name)  # stale registry entry — file deleted out of band
        return _fail(
            f"Skill '{name}' file no longer exists at {plugin_skill_md}. The registry entry "
            f"has been cleaned up — try again after the plugin is reloaded."), None
    if plugin_skill_md is not None:
        return _serve_plugin_skill(
            plugin_skill_md, namespace, bare, file_path=file_path, preprocess=preprocess, session_id=task_id), None
    if available := pm.list_plugin_skills(namespace):  # plugin exists but this specific skill is missing
        return _fail(
            f"Skill '{bare}' not found in plugin '{namespace}'.",
            available_skills=[f"{namespace}:{s}" for s in available],
            hint=f"The '{namespace}' plugin provides {len(available)} skill(s)."), None
    return None, (f"{namespace}/{bare}" if bare else None)  # plugin not found → local scan


def _under_any(path: Path, dirs) -> bool:
    """True when ``path`` (resolved where possible) lives under one of ``dirs``."""
    resolved = path
    with suppress(Exception):
        resolved = path.resolve()
    return any(resolved.is_relative_to(d) for d in dirs)


def _is_package_owned_markdown(path: Path, search_root: Path) -> bool:
    """True when a legacy Markdown candidate belongs to an ancestor directory skill."""
    try:
        relative = path.relative_to(search_root)
    except ValueError:
        return False
    return any(
        (search_root.joinpath(*relative.parts[:depth]) / "SKILL.md").is_file()
        for depth in range(1, len(relative.parts))
    )


def _collect_skill_candidates(name, local_category_name, all_dirs):
    """ALL (skill_dir, skill_md) candidates across every dir and lookup strategy (direct path,
    recursive by dir / frontmatter name, legacy flat <name>.md), deduped by resolved path.
    Collision detection is the point: silent shadowing of a local skill by a same-named
    external one is a real bug class, so the caller refuses >1."""
    from agent.skill_utils import iter_skill_index_files
    candidates: List[Tuple[Optional[Path], Path]] = []
    seen_md: set = set()

    def _record(sd: Optional[Path], smd: Path) -> None:
        key = smd
        with suppress(Exception):
            key = smd.resolve()
        if key not in seen_md:
            seen_md.add(key)
            candidates.append((sd, smd))

    def _record_direct(direct_path: Path, search_root: Path) -> None:  # "mlops/axolotl" / "axolotl" or its flat .md sibling
        flat = direct_path.with_suffix(".md")
        if not _is_skill_support_path(direct_path) and direct_path.is_dir() and (direct_path / "SKILL.md").exists():
            _record(direct_path, direct_path / "SKILL.md")
        elif (flat.exists() and not _is_skill_support_path(flat)
              and not _is_package_owned_markdown(flat, search_root)):
            _record(None, flat)

    for search_dir in all_dirs:
        for direct in filter(None, (name, local_category_name)):  # "p:x" with no plugin p → "p/x"
            _record_direct(search_dir / direct, search_dir)
        # Recursive by directory name plus frontmatter `name:` — skills_list()
        # exposes the frontmatter name, so skill_view(name) must accept it too.
        for found_skill_md in iter_skill_index_files(search_dir, "SKILL.md"):
            if (found_skill_md.parent.name == name
                    or _safe_frontmatter(found_skill_md).get("name") == name):
                _record(found_skill_md.parent, found_skill_md)
        # Legacy flat <name>.md anywhere under the dir. Markdown owned by an ancestor
        # directory skill loads through file_path and must not shadow a real skill.
        for found_md in search_dir.rglob(f"{name}.md"):
            if (found_md.name != "SKILL.md" and not _is_skill_support_path(found_md)
                    and not _is_package_owned_markdown(found_md, search_dir)):
                _record(None, found_md)
    return candidates


# (support dir, globs, recursive, files only) — order is the linked_files key order.
_LINKED_FILE_SPECS = (
    ("references", ["*.md"], False, False),
    ("templates", ["*.md", "*.py", "*.yaml", "*.yml", "*.json", "*.tex", "*.sh"], True, False),
    ("assets", ["*"], True, True),
    ("scripts", ["*.py", "*.sh", "*.bash", "*.js", "*.ts", "*.rb"], False, False))


def _skill_linked_files(skill_dir: Optional[Path]) -> dict:
    """references/templates/assets/scripts of a directory skill (empty groups dropped)."""
    files: dict = {}
    for sub, globs, recursive, files_only in _LINKED_FILE_SPECS if skill_dir else ():
        base = skill_dir / sub
        found = [
            f.relative_to(skill_dir).as_posix() for g in globs if base.exists()
            for f in (base.rglob(g) if recursive else base.glob(g))
            if not files_only or f.is_file()]
        if found:
            files[sub] = found
    return files


def _org_provenance_header(skill_dir: Path, active_skills_dir: Path):
    """(org_provenance dict, header text) for an org-mirror skill, else (None, ""). Announced IN
    the content the model consumes; the author is token-verified at push time by the sync plane."""
    from agent.skill_utils import ORG_PROVENANCE_FILE, is_org_mirror_path, org_id_of_path
    if not is_org_mirror_path(skill_dir, active_skills_dir):
        return None, ""
    prov_org = org_id_of_path(skill_dir, active_skills_dir)
    prov: dict = {}
    if prov_org:
        with suppress(Exception):
            prov_path = active_skills_dir / "_org" / prov_org / ORG_PROVENANCE_FILE
            loaded = json.loads(_read_skill_text(prov_path))
            prov = loaded if isinstance(loaded, dict) else {}
    author = str(prov.get("author_device") or prov.get("author_user_id") or "")
    ts = str(prov.get("ts") or "")
    header = (
        "> [!NOTE] ORG-SHARED SKILL — provenance\n"
        f"> This skill is shared by your organisation (org `{prov_org}`"
        + (f", last updated by `{author}`" if author else "")
        + (f", as of {ts}" if ts else "")
        + "). It was reviewed and approved for the whole\n"
        "> team — treat it as third-party instructions rather than your own notes.\n"
        "> You MAY improve it in place like any other skill. Your edits are kept locally\n"
        "> and are never overwritten by org updates; share them back with\n"
        "> `hermes sync propose` (or automatically, if your org enables it).\n\n")
    return {"org_id": prov_org, "shared_by": author or None, "as_of": ts or None}, header


def _skill_readiness(frontmatter: Dict[str, Any], skill_name: str) -> Tuple[dict, dict]:
    """Resolve required env vars / credential files (prompting for secrets where the surface
    allows) and register what's available for sandboxes. Returns ``(fields, extras)``: fields go
    before ``_source_path`` in the skill_view result, extras after — key order is tool output."""
    required_env_vars = _get_required_environment_variables(frontmatter)
    from tools.terminal_scope import terminal_env
    backend = str(terminal_env("TERMINAL_ENV", "local")).strip().lower() or "local"
    env_snapshot = load_env()
    missing_required_env_vars = [
        e for e in required_env_vars
        if not e.get("optional") and not _is_env_var_persisted(e["name"], env_snapshot)]
    capture_result = _capture_required_environment_variables(skill_name, missing_required_env_vars)
    if missing_required_env_vars:  # re-read: a successful capture persisted into .env
        env_snapshot = load_env()
    still_missing = set(capture_result["missing_names"])
    remaining = [
        e["name"] for e in required_env_vars if not e.get("optional")
        and (e["name"] in still_missing or not _is_env_var_persisted(e["name"], env_snapshot))]
    setup_needed = bool(remaining)
    # Only vars actually set pass through to sandboxed execution (execute_code, terminal).
    if available_env_names := [e["name"] for e in required_env_vars if e["name"] not in remaining]:
        try:
            from tools.env_passthrough import register_env_passthrough
            register_env_passthrough(available_env_names)
        except Exception:
            logger.debug("Could not register env passthrough for skill %s", skill_name, exc_info=True)
    # Credential files for remote sandboxes: existing host files are registered,
    # missing ones flag setup_needed.
    required_cred_files_raw = frontmatter.get("required_credential_files", [])
    missing_cred_files: list = []
    if isinstance(required_cred_files_raw, list) and required_cred_files_raw:
        try:
            from tools.credential_files import register_credential_files
            missing_cred_files = register_credential_files(required_cred_files_raw)
            setup_needed = setup_needed or bool(missing_cred_files)
        except Exception:
            logger.debug("Could not register credential files for skill %s", skill_name, exc_info=True)
    status = SkillReadinessStatus.SETUP_NEEDED if setup_needed else SkillReadinessStatus.AVAILABLE
    fields = {
        "required_environment_variables": required_env_vars, "required_commands": [],
        "missing_required_environment_variables": remaining,
        "missing_credential_files": missing_cred_files, "missing_required_commands": [],
        "setup_needed": setup_needed, "setup_skipped": capture_result["setup_skipped"],
        "readiness_status": status.value}
    extras: dict = {}
    if setup_help := next((e["help"] for e in required_env_vars if e.get("help")), None):
        extras["setup_help"] = setup_help
    if capture_result["gateway_setup_hint"]:
        extras["gateway_setup_hint"] = capture_result["gateway_setup_hint"]
    missing_items = [f"env ${n}" for n in remaining] + [f"file {p}" for p in missing_cred_files]
    if setup_needed and (setup_note := _build_setup_note(status, missing_items, setup_help)):
        if _is_remote_env_backend(backend):
            setup_note = f"{setup_note} {backend.upper()}-backed skills need these requirements available inside the remote environment as well."
        extras["setup_note"] = setup_note
    return fields, extras


def _owning_search_dir(skill_md: Path, all_dirs) -> Optional[Path]:
    """Most specific search dir containing *skill_md*, compared lexically: a symlinked entry
    belongs to the root that exposes it, not to the root its target lives in."""
    owners = [Path(d) for d in all_dirs if skill_md.is_relative_to(d)]
    return max(owners, key=lambda d: len(d.parts), default=None)


def _rank_same_root_candidate(candidate, root: Path) -> tuple:
    """Real SKILL.md beats a legacy flat ``<name>.md``, then the shallower path wins."""
    _skill_dir, skill_md = candidate
    return (skill_md.name != "SKILL.md", len(skill_md.relative_to(root).parts))


def _provably_same_skill(candidates) -> bool:
    """True only when every candidate is the SAME skill: one resolved SKILL.md (symlink view)
    or byte-identical content (copy). Anything else is two different skills sharing a name,
    and picking one by depth would let ``<root>/evil`` (``name: github``) shadow the real one."""
    try:
        if len({os.path.realpath(smd) for _sd, smd in candidates}) == 1:
            return True
        return len({hashlib.sha256(smd.read_bytes()).hexdigest() for _sd, smd in candidates}) == 1
    except OSError:
        return False


def _locate_skill(name: str, local_category_name: Optional[str], project_dirs: list, all_dirs):
    """Unique on-disk skill for *name*: collision refusal, project-tier precedence, same-root
    precedence, quarantine gate, not-found listing. ``(error_json, skill_dir, skill_md)``;
    skill_md set iff no error."""
    if not all_dirs:
        return _fail(
            "Skills directory does not exist yet. It will be created on first install."), None, None
    candidates = _collect_skill_candidates(name, local_category_name, all_dirs)
    if len(candidates) > 1 and project_dirs:
        # A project skill intentionally overrides a same-named local/external skill;
        # ambiguity WITHIN the project tier (two different skills) still refuses.
        candidates = [c for c in candidates if _under_any(c[1], project_dirs)] or candidates
    if len(candidates) > 1:
        # The refusal below guards against one skill silently shadowing another. Copies of ONE
        # skill inside a single search dir (``<root>/x`` symlink view + ``<root>/cat/x`` copy)
        # shadow nothing, so rank them instead; different content, an equal-rank tie or a
        # cross-tier spread still refuses.
        roots = {_owning_search_dir(smd, all_dirs) for _sd, smd in candidates}
        if len(roots) == 1 and None not in roots and _provably_same_skill(candidates):
            root = roots.pop()
            ranked = sorted(candidates, key=lambda c: _rank_same_root_candidate(c, root))
            if _rank_same_root_candidate(ranked[0], root) != _rank_same_root_candidate(ranked[1], root):
                logger.info("Skill '%s': %d identical same-root copies, resolved to %s (duplicates: %s)",
                            name, len(candidates), ranked[0][1],
                            "; ".join(str(smd) for _sd, smd in ranked[1:]))
                candidates = [ranked[0]]
    if len(candidates) > 1:
        paths = [str(smd) for _, smd in candidates]
        logger.warning("Skill name collision for '%s': %d candidates — %s", name, len(candidates), "; ".join(paths))
        return _fail(
            f"Ambiguous skill name '{name}': {len(candidates)} skills match across your local skills dir "
            "and external_dirs. Refusing to guess — load one explicitly by its categorized path.",
            matches=paths,
            hint="Pass the full relative path instead of the bare name (e.g., 'category/skill-name'), "
            "or rename one of the colliding skills so each name is unique."), None, None
    skill_dir, skill_md = candidates[0] if candidates else (None, None)
    # Quarantine gate: a project-tier skill with a dangerous scan verdict must not
    # load even by explicit name (same chokepoint the index and skills_list use).
    if skill_md is not None and project_dirs:
        from agent.skill_utils import is_quarantined_project_skill
        if _under_any(skill_md, project_dirs) and is_quarantined_project_skill(skill_md):
            return _fail(
                f"Project skill '{name}' is quarantined: the security scan flagged its content as "
                "dangerous. It will not load until the repo's skill content changes and passes a re-scan.",
                hint="Inspect the skill in the repo checkout, or untrust the repo with "
                "`hermes skills untrust`."), None, None
    if not skill_md or not skill_md.exists():
        available = [s["name"] for s in _sort_skills(_find_all_skills())[:20]]
        return _fail(f"Skill '{name}' not found.", available_skills=available,
                     hint="Use skills_list to see all available skills"), None, None
    return None, skill_dir, skill_md


def _log_security_warnings(name: str, skill_md: Path, content: str, all_dirs, active_skills_dir):
    """Warn (never block) when loaded from outside the trusted dirs (project + local + external)
    and/or when common prompt-injection patterns appear. The check is on the RESOLVED path:
    every candidate is built as ``<search_dir>/...`` so a lexical test can never fire, and a
    SKILL.md symlinked to a file outside every root is exactly what this guards against."""
    trusted_dirs = [active_skills_dir.resolve()]
    with suppress(Exception):
        trusted_dirs.extend(d.resolve() for d in all_dirs)
    warnings = []
    if not _under_any(skill_md, trusted_dirs):
        warnings.append(f"skill file is outside the trusted skills directory (~/.hermes/skills/): {skill_md}")
    if any(p in content.lower() for p in _INJECTION_PATTERNS):
        warnings.append("skill content contains patterns that may indicate prompt injection")
    if warnings:
        logger.warning("Skill security warning for '%s': %s", name, "; ".join(warnings))


def skill_view(
    name: str,
    file_path: str = None,
    task_id: str = None,
    preprocess: bool = True,
    _viewability_probe: bool = False,
) -> str:
    """
    View the content of a skill or a specific file within a skill directory.

    Args:
        name: Name or path of the skill (e.g., "axolotl" or "03-fine-tuning/axolotl").
            Qualified names like "plugin:skill" resolve to plugin-provided skills.
        file_path: Optional path to a specific file within the skill (e.g., "references/api.md")
        task_id: Optional task identifier used to probe the active backend
        preprocess: Apply configured SKILL.md template and inline shell rendering
            to main skill content. Internal slash/preload callers disable this
            because they render the skill message themselves.

    Returns:
        JSON string with skill content or error message
    """
    try:
        # Validate before the ':' qualified-name dispatch so a Windows drive
        # path (e.g. C:\skills\foo) can't be reinterpreted as a plugin
        # namespace, and so a traversal/absolute name never reaches the
        # search-dir join that builds direct_path below.
        lookup_error = _skill_lookup_path_error(name)
        if lookup_error:
            return json.dumps(
                {
                    "success": False,
                    "error": lookup_error,
                    "hint": "Use a skill name or relative path within the skills directory.",
                },
                ensure_ascii=False,
            )

        local_category_name: str | None = None
        protected_call = _protected_skill_search_dirs(task_id, []) is not None
        if protected_call:
            # Protected runs may read authorized skill resources, but must not
            # turn skill preprocessing into host-side command execution.
            preprocess = False
        if protected_call and ":" in name:
            from agent.skill_utils import is_valid_namespace, parse_qualified_name

            namespace, bare = parse_qualified_name(name)
            if not is_valid_namespace(namespace):
                return json.dumps(
                    {
                        "success": False,
                        "error": f"Invalid namespace '{namespace}' in '{name}'.",
                    },
                    ensure_ascii=False,
                )
            local_category_name = f"{namespace}/{bare}"
            name = local_category_name

        # ── Qualified name dispatch (plugin skills) ──────────────────
        # Names containing ':' are routed to the plugin skill registry.
        # Bare names fall through to the existing flat-tree scan below.
        if ":" in name:
            from agent.skill_utils import is_valid_namespace, parse_qualified_name
            from hermes_cli.plugins import discover_plugins, get_plugin_manager

            namespace, bare = parse_qualified_name(name)
            if not is_valid_namespace(namespace):
                return json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"Invalid namespace '{namespace}' in '{name}'. "
                            f"Namespaces must match [a-zA-Z0-9_-]+."
                        ),
                    },
                    ensure_ascii=False,
                )

            discover_plugins()  # idempotent
            pm = get_plugin_manager()
            active_memory_provider = None
            try:
                from plugins.memory import (
                    _get_active_memory_provider,
                    _prune_inactive_memory_provider_skills,
                )

                active_memory_provider = _get_active_memory_provider()
                _prune_inactive_memory_provider_skills(active_memory_provider)
            except Exception as exc:
                logger.debug(
                    "Failed pruning inactive memory-provider skills: %s",
                    exc,
                )

            plugin_skill_md = pm.find_plugin_skill(name)

            # Memory provider plugins are loaded through plugins.memory rather
            # than the general PluginManager. If a memory provider shim also
            # registers skills, load the namespaced provider once so its
            # collector can forward those skills into the plugin skill registry
            # before declaring the qualified skill missing.
            if plugin_skill_md is None:
                try:
                    from plugins.memory import load_memory_provider

                    if namespace == active_memory_provider:
                        load_memory_provider(namespace)
                        plugin_skill_md = pm.find_plugin_skill(name)
                except Exception as exc:
                    logger.debug(
                        "Failed lazy memory-provider skill load for %s: %s",
                        namespace,
                        exc,
                    )

            if plugin_skill_md is not None:
                if _protected_skill_path_allowed(task_id, plugin_skill_md) is False:
                    return json.dumps(
                        {
                            "success": False,
                            "error": (
                                f"Skill '{name}' is outside this protected attempt's "
                                "filesystem grants."
                            ),
                        },
                        ensure_ascii=False,
                    )
                if not plugin_skill_md.exists():
                    # Stale registry entry — file deleted out of band
                    pm.remove_plugin_skill(name)
                    return json.dumps(
                        {
                            "success": False,
                            "error": (
                                f"Skill '{name}' file no longer exists at "
                                f"{plugin_skill_md}. The registry entry has "
                                f"been cleaned up — try again after the "
                                f"plugin is reloaded."
                            ),
                        },
                        ensure_ascii=False,
                    )
                return _serve_plugin_skill(
                    plugin_skill_md,
                    namespace,
                    bare,
                    file_path=file_path,
                    preprocess=preprocess,
                    session_id=task_id,
                )

            # Plugin exists but this specific skill is missing?
            available = pm.list_plugin_skills(namespace)
            if available:
                return json.dumps(
                    {
                        "success": False,
                        "error": f"Skill '{bare}' not found in plugin '{namespace}'.",
                        "available_skills": [f"{namespace}:{s}" for s in available],
                        "hint": f"The '{namespace}' plugin provides {len(available)} skill(s).",
                    },
                    ensure_ascii=False,
                )
            # Plugin itself not found — fall through to flat-tree scan.
            # Categorized local skills also use `category:skill` in config and
            # gateway prompts, so preserve that form and translate it to the
            # on-disk `category/skill` path during the local scan below.
            if bare:
                local_category_name = f"{namespace}/{bare}"

        from agent.skill_utils import get_external_skills_dirs, get_project_skills_dirs

        # The categorized fall-through form (namespace/bare) joins onto each
        # search dir too; re-validate it since `bare` is not namespace-checked.
        if local_category_name:
            lookup_error = _skill_lookup_path_error(local_category_name)
            if lookup_error:
                return json.dumps(
                    {
                        "success": False,
                        "error": lookup_error,
                        "hint": "Use a skill name or relative path within the skills directory.",
                    },
                    ensure_ascii=False,
                )

        # Build list of all skill directories to search. Project dirs first —
        # they're the highest-precedence tier and the collision resolver
        # below uses this ordering.
        project_dirs = get_project_skills_dirs()
        all_dirs = list(project_dirs)
        active_skills_dir = _skills_dir()
        if active_skills_dir.exists():
            all_dirs.append(active_skills_dir)
        all_dirs.extend(get_external_skills_dirs())
        protected_dirs = _protected_skill_search_dirs(task_id, all_dirs)
        if protected_dirs is not None:
            all_dirs = protected_dirs

        if not all_dirs:
            return json.dumps(
                {
                    "success": False,
                    "error": "Skills directory does not exist yet. It will be created on first install.",
                },
                ensure_ascii=False,
            )

        skill_dir = None
        skill_md = None

        # Collision detection: collect ALL candidates across every dir using
        # every lookup strategy (direct path, recursive by parent dir name,
        # legacy flat <name>.md). If more than one matches, refuse and tell
        # the caller — silent shadowing of a local skill by a same-named
        # external skill is a real bug class (`/skills` shows one, agent
        # loaded the other) so we surface it loudly instead of guessing.
        from agent.skill_utils import iter_skill_index_files

        candidates: List[Tuple[Optional[Path], Path]] = []  # (skill_dir, skill_md)
        seen_md: set = set()

        def _record(sd: Optional[Path], smd: Path) -> None:
            if (
                protected_dirs is not None
                and _protected_skill_path_allowed(task_id, smd) is not True
            ):
                return
            try:
                key = smd.resolve()
            except Exception:
                key = smd
            if key in seen_md:
                return
            seen_md.add(key)
            candidates.append((sd, smd))

        for search_dir in all_dirs:
            # Strategy 1: direct path (e.g., "mlops/axolotl" or bare "axolotl"
            # at the top of the dir).
            direct_path = search_dir / name
            if (
                not _is_skill_support_path(direct_path)
                and direct_path.is_dir()
                and (direct_path / "SKILL.md").exists()
            ):
                _record(direct_path, direct_path / "SKILL.md")
            elif direct_path.with_suffix(".md").exists() and not _is_skill_support_path(
                direct_path.with_suffix(".md")
            ) and not _is_package_owned_markdown(
                direct_path.with_suffix(".md"), search_dir
            ):
                _record(None, direct_path.with_suffix(".md"))

            # Strategy 1b: categorized form for plugin namespace fall-through
            # (e.g., a "myplugin:explore" name with no plugin registered also
            # tries the on-disk path "myplugin/explore").
            if local_category_name:
                categorized_path = search_dir / local_category_name
                if (
                    not _is_skill_support_path(categorized_path)
                    and categorized_path.is_dir()
                    and (categorized_path / "SKILL.md").exists()
                ):
                    _record(categorized_path, categorized_path / "SKILL.md")
                elif categorized_path.with_suffix(
                    ".md"
                ).exists() and not _is_skill_support_path(
                    categorized_path.with_suffix(".md")
                ) and not _is_package_owned_markdown(
                    categorized_path.with_suffix(".md"), search_dir
                ):
                    _record(None, categorized_path.with_suffix(".md"))

            # Strategy 2: recursive by directory name (catches nested skills
            # like "foundations/runtime/explore-codebase" called by bare name),
            # plus frontmatter `name:` lookup. `skills_list()` exposes the
            # frontmatter name, so `skill_view(name)` must accept it too even
            # when the on-disk directory is a shorter category/alias.
            for found_skill_md in iter_skill_index_files(search_dir, "SKILL.md"):
                if (
                    protected_dirs is not None
                    and _protected_skill_path_allowed(task_id, found_skill_md) is not True
                ):
                    continue
                if found_skill_md.parent.name == name:
                    _record(found_skill_md.parent, found_skill_md)
                    continue
                try:
                    fm_content = found_skill_md.read_text(encoding="utf-8-sig", errors="replace")
                    fm, _ = _parse_frontmatter(fm_content)
                except Exception:
                    fm = {}
                if fm.get("name") == name:
                    _record(found_skill_md.parent, found_skill_md)

            # Strategy 3: legacy flat <name>.md files anywhere under the dir.
            # Exclude skill support docs: references/templates/assets/scripts
            # are loaded through skill_view(skill, file_path=...) and must not
            # shadow or collide with real skills that share the same basename.
            for found_md in search_dir.rglob(f"{name}.md"):
                if found_md.name != "SKILL.md" and not _is_skill_support_path(
                    found_md
                ) and not _is_package_owned_markdown(found_md, search_dir):
                    _record(None, found_md)

        if len(candidates) > 1 and project_dirs:
            # Cross-tier collision resolution: a project skill intentionally
            # overrides a same-named local/external skill, so when at least
            # one candidate lives under a trusted project dir, narrow to
            # those. Ambiguity WITHIN the project tier still refuses below.
            def _in_project(smd: Path) -> bool:
                try:
                    resolved = smd.resolve()
                except Exception:
                    resolved = smd
                for pd in project_dirs:
                    try:
                        resolved.relative_to(pd)
                        return True
                    except ValueError:
                        continue
                return False

            project_candidates = [
                (sd, smd) for sd, smd in candidates if _in_project(smd)
            ]
            if project_candidates:
                candidates = project_candidates

        if len(candidates) > 1:
            roots = {_owning_search_dir(smd, all_dirs) for _sd, smd in candidates}
            if len(roots) == 1 and None not in roots and _provably_same_skill(candidates):
                root = roots.pop()
                ranked = sorted(candidates, key=lambda c: _rank_same_root_candidate(c, root))
                if _rank_same_root_candidate(ranked[0], root) != _rank_same_root_candidate(ranked[1], root):
                    candidates = [ranked[0]]

        if len(candidates) > 1:
            paths = [str(smd) for _, smd in candidates]
            logging.getLogger(__name__).warning(
                "Skill name collision for '%s': %d candidates — %s",
                name, len(candidates), "; ".join(paths),
            )
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Ambiguous skill name '{name}': {len(candidates)} skills "
                        "match across your local skills dir and external_dirs. "
                        "Refusing to guess — load one explicitly by its categorized path."
                    ),
                    "matches": paths,
                    "hint": (
                        "Pass the full relative path instead of the bare name "
                        "(e.g., 'category/skill-name'), or rename one of the "
                        "colliding skills so each name is unique."
                    ),
                },
                ensure_ascii=False,
            )

        if candidates:
            skill_dir, skill_md = candidates[0]

        # Quarantine gate: a project-tier skill with a dangerous scan verdict
        # must not load even by explicit name (same chokepoint the index and
        # skills_list use — see agent.skill_utils.iter_project_skill_files).
        if skill_md is not None and project_dirs:
            from agent.skill_utils import is_quarantined_project_skill

            def _under_project(p: Path) -> bool:
                try:
                    rp = p.resolve()
                except Exception:
                    rp = p
                for pd in project_dirs:
                    try:
                        rp.relative_to(pd)
                        return True
                    except ValueError:
                        continue
                return False

            if _under_project(skill_md) and is_quarantined_project_skill(skill_md):
                return json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"Project skill '{name}' is quarantined: the security "
                            "scan flagged its content as dangerous. It will not "
                            "load until the repo's skill content changes and "
                            "passes a re-scan."
                        ),
                        "hint": (
                            "Inspect the skill in the repo checkout, or untrust "
                            "the repo with `hermes skills untrust`."
                        ),
                    },
                    ensure_ascii=False,
                )

        if not skill_md or not skill_md.exists():
            available = [
                s["name"]
                for s in _sort_skills(
                    _find_all_skills(
                        search_dirs=protected_dirs,
                        task_id=task_id if protected_dirs is not None else None,
                    )
                )[:20]
            ]
            return json.dumps(
                {
                    "success": False,
                    "error": f"Skill '{name}' not found.",
                    "available_skills": available,
                    "hint": "Use skills_list to see all available skills",
                },
                ensure_ascii=False,
            )

        # Read the file once — reused for platform check and main content below
        if (
            protected_dirs is not None
            and _protected_skill_path_allowed(task_id, skill_md) is not True
        ):
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Skill '{name}' is outside this protected attempt's "
                        "filesystem grants."
                    ),
                },
                ensure_ascii=False,
            )
        try:
            content = skill_md.read_text(encoding="utf-8-sig", errors="replace")
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Failed to read skill '{name}': {e}",
                },
                ensure_ascii=False,
            )

        # Security: warn if skill is loaded from outside trusted directories
        # (project dirs + local skills dir + configured external_dirs — i.e.
        # everything in all_dirs — are trusted)
        _outside_skills_dir = True
        try:
            _trusted_dirs = [directory.resolve() for directory in all_dirs]
        except Exception:
            _trusted_dirs = []
        for _td in _trusted_dirs:
            try:
                skill_md.resolve().relative_to(_td)
                _outside_skills_dir = False
                break
            except ValueError:
                continue

        # Security: detect common prompt injection patterns
        # (pattern list at module level as _INJECTION_PATTERNS)
        _content_lower = content.lower()
        _injection_detected = any(p in _content_lower for p in _INJECTION_PATTERNS)

        if _outside_skills_dir or _injection_detected:
            _warnings = []
            if _outside_skills_dir:
                _warnings.append(f"skill file is outside the trusted skills directory (~/.hermes/skills/): {skill_md}")
            if _injection_detected:
                _warnings.append("skill content contains patterns that may indicate prompt injection")
            logging.getLogger(__name__).warning("Skill security warning for '%s': %s", name, "; ".join(_warnings))

        parsed_frontmatter: Dict[str, Any] = {}
        try:
            parsed_frontmatter, _ = _parse_frontmatter(content)
        except Exception:
            parsed_frontmatter = {}

        if not skill_matches_platform(parsed_frontmatter):
            return json.dumps(
                {
                    "success": False,
                    "error": f"Skill '{name}' is not supported on this platform.",
                    "readiness_status": SkillReadinessStatus.UNSUPPORTED.value,
                },
                ensure_ascii=False,
            )

        # Check if the skill is disabled by the user
        resolved_name = parsed_frontmatter.get("name", skill_md.parent.name)
        if _is_skill_disabled(resolved_name):
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Skill '{resolved_name}' is disabled. "
                        "Enable it with `hermes skills` or inspect the files directly on disk."
                    ),
                },
                ensure_ascii=False,
            )

        if _viewability_probe:
            return json.dumps(
                {"success": True, "name": resolved_name},
                ensure_ascii=False,
            )

        # If a specific file path is requested, read that instead
        if file_path and skill_dir:
            from tools.path_security import validate_within_dir, has_traversal_component

            # Security: Prevent path traversal attacks
            if has_traversal_component(file_path):
                return json.dumps(
                    {
                        "success": False,
                        "error": "Path traversal ('..') is not allowed.",
                        "hint": "Use a relative path within the skill directory",
                    },
                    ensure_ascii=False,
                )

            target_file = skill_dir / file_path

            # Security: Verify resolved path is still within skill directory
            traversal_error = validate_within_dir(target_file, skill_dir)
            if traversal_error:
                return json.dumps(
                    {
                        "success": False,
                        "error": traversal_error,
                        "hint": "Use a relative path within the skill directory",
                    },
                    ensure_ascii=False,
                )
            if (
                protected_dirs is not None
                and _protected_skill_path_allowed(task_id, target_file) is not True
            ):
                return json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"File '{file_path}' is outside this protected attempt's "
                            "filesystem grants."
                        ),
                    },
                    ensure_ascii=False,
                )
            if not target_file.is_file():
                # List available files in the skill directory, organized by type
                available_files = {
                    "references": [],
                    "templates": [],
                    "assets": [],
                    "scripts": [],
                    "other": [],
                }

                # Scan for all readable files
                for f in skill_dir.rglob("*"):
                    if f.is_file() and f.name != "SKILL.md":
                        rel = str(f.relative_to(skill_dir))
                        if rel.startswith("references/"):
                            available_files["references"].append(rel)
                        elif rel.startswith("templates/"):
                            available_files["templates"].append(rel)
                        elif rel.startswith("assets/"):
                            available_files["assets"].append(rel)
                        elif rel.startswith("scripts/"):
                            available_files["scripts"].append(rel)
                        elif f.suffix in {
                            ".md",
                            ".py",
                            ".yaml",
                            ".yml",
                            ".json",
                            ".tex",
                            ".sh",
                        }:
                            available_files["other"].append(rel)

                # Remove empty categories
                available_files = {k: v for k, v in available_files.items() if v}

                return json.dumps(
                    {
                        "success": False,
                        "error": f"File '{file_path}' not found in skill '{name}'.",
                        "available_files": available_files,
                        "hint": "Use one of the available file paths listed above",
                    },
                    ensure_ascii=False,
                )

            # Read the file content
            try:
                content = target_file.read_text(encoding="utf-8-sig", errors="replace")
            except UnicodeDecodeError:
                # Binary file - return info about it instead
                return json.dumps(
                    {
                        "success": True,
                        "name": name,
                        "file": file_path,
                        "content": f"[Binary file: {target_file.name}, size: {target_file.stat().st_size} bytes]",
                        "is_binary": True,
                    },
                    ensure_ascii=False,
                )

            _mark_background_review_read(target_file)

            return json.dumps(
                {
                    "success": True,
                    "name": name,
                    "file": file_path,
                    "content": content,
                    "file_type": target_file.suffix,
                    # Internal: absolute source path for the repeat-view dedup
                    # fingerprint (mtime+size change detection).
                    "_source_path": str(target_file),
                },
                ensure_ascii=False,
            )

        # Reuse the parse from the platform check above
        frontmatter = parsed_frontmatter

        # Discover linked files with per-scan protected authority validation.
        linked_files = {}
        if skill_dir:
            linked_scope_valid, linked_files = _discover_skill_linked_files(
                skill_dir,
                task_id=task_id,
                protected=protected_dirs is not None,
            )
            if not linked_scope_valid:
                return json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"Skill '{name}' changed outside this protected attempt's "
                            "filesystem authority during access."
                        ),
                    },
                    ensure_ascii=False,
                )
        # Check metadata.hermes.* first (agentskills.io convention), fall back to top-level
        hermes_meta = {}
        metadata = frontmatter.get("metadata")
        if isinstance(metadata, dict):
            hermes_meta = metadata.get("hermes", {}) or {}

        tags = _parse_tags(hermes_meta.get("tags") or frontmatter.get("tags", ""))
        related_skills = _parse_tags(
            hermes_meta.get("related_skills") or frontmatter.get("related_skills", "")
        )

        # linked_files was built by the bounded discovery helper above.

        try:
            rel_path = str(skill_md.relative_to(active_skills_dir))
        except ValueError:
            # External skill — use path relative to the skill's own parent dir
            rel_path = str(skill_md.relative_to(skill_md.parent.parent)) if skill_md.parent.parent else skill_md.name
        skill_name = frontmatter.get("name", skill_md.stem if not skill_dir else skill_dir.name)
        readiness, readiness_extras = _skill_readiness(frontmatter, skill_name)
        rendered_content = content if not preprocess else _preprocess_skill(
            content, skill_dir, task_id, "Could not preprocess skill content for %s", skill_name)
        org_provenance, header = None, ""
        if skill_dir:
            try:
                org_provenance, header = _org_provenance_header(skill_dir, active_skills_dir)
            except Exception:
                logger.debug("Could not resolve org provenance for %s", skill_name, exc_info=True)

        # ── pm tool deps (`deps: [ffmpeg]` frontmatter) ──────────────
        # Loading the skill IS the activation moment: ensure each declared
        # pm package now so the skill's commands work when the model runs
        # them. Failure never blocks the skill content — the note carries
        # the remedy.
        deps_note = None
        declared_deps = frontmatter.get("deps") or []
        if isinstance(declared_deps, str):
            declared_deps = [declared_deps]
        if isinstance(declared_deps, list) and declared_deps:
            failed_deps = []
            for dep in [str(d).strip() for d in declared_deps if str(d).strip()]:
                try:
                    import pm

                    pm.ensure(dep)
                except Exception as exc:
                    failed_deps.append(f"{dep}: {exc}")
            if failed_deps:
                deps_note = (
                    "Tool dependencies could not be installed — "
                    + "; ".join(failed_deps)
                    + ". Run `hermes pm install "
                    + " ".join(str(d) for d in declared_deps)
                    + "` and reload."
                )

        result = {
            "success": True, "name": skill_name, "description": frontmatter.get("description", ""),
            "tags": tags, "related_skills": related_skills, "content": header + rendered_content,
            "path": rel_path, "skill_dir": str(skill_dir) if skill_dir else None,
            "org_provenance": org_provenance,
            "linked_files": linked_files if linked_files else None,
            "usage_hint": "To view linked files, call skill_view(name, file_path) where file_path is e.g. 'references/api.md' or 'assets/config.yaml'" if linked_files else None,
            **readiness,
            # Internal: absolute source path for the repeat-view dedup fingerprint.
            "_source_path": str(skill_md),
            **readiness_extras}
        if deps_note:
            result["deps_note"] = deps_note
        _mark_background_review_read(skill_md)
        if frontmatter.get("compatibility"):  # agentskills.io optional fields
            result["compatibility"] = frontmatter["compatibility"]
        if isinstance(metadata, dict):
            result["metadata"] = metadata
        return _json(result)
    except Exception as e:
        return tool_error(str(e), success=False)


SKILLS_LIST_SCHEMA = {
    "name": "skills_list",
    "description": "List available skills (name + description). Use skill_view(name) to load full content.",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional category filter to narrow results",
            }
        },
        "required": [],
    },
}

SKILL_VIEW_SCHEMA = {
    "name": "skill_view",
    "description": "Skills allow for loading information about specific tasks and workflows, as well as scripts and templates. Load a skill's full content or access its linked files (references, templates, scripts). First call returns SKILL.md content plus a 'linked_files' dict showing available references/templates/scripts. To access those, call again with file_path parameter.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The skill name (use skills_list to see available skills). For plugin-provided skills, use the qualified form 'plugin:skill' (e.g. 'superpowers:writing-plans').",
            },
            "file_path": {
                "type": "string",
                "description": "OPTIONAL: Path to a linked file within the skill (e.g., 'references/api.md', 'templates/config.yaml', 'scripts/validate.py'). Omit to get the main SKILL.md content.",
            },
        },
        "required": ["name"],
    },
}

registry.register(
    name="skills_list", toolset="skills", schema=SKILLS_LIST_SCHEMA,
    handler=lambda args, **kw: skills_list(category=args.get("category"), task_id=kw.get("task_id")),
    check_fn=check_skills_requirements, emoji="📚")


def _skill_view_with_bump(args, **kw):
    """Invoke skill_view, then bump view_count/use on success (best-effort). Repeat-view dedup
    mirrors read_file's unchanged-stub: a SAME, unchanged skill file already loaded in this
    session returns a short stub (cache cleared on context compression)."""
    name = args.get("name", "")
    task_id = kw.get("task_id")
    # The background-review fork shares the parent's task_id (prefix-cache parity). A stub there
    # (a) skips the read-mark its read-before-write guard requires and (b) lets it patch from a
    # possibly-pruned transcript copy (#95976). No dedup in the fork; None also keeps its views
    # out of the parent's bucket.
    dedup_task_id = None if is_background_review() else task_id
    if (stub := _check_skill_view_dedup(dedup_task_id, name, args.get("file_path"))) is not None:
        return stub
    result = skill_view(name, file_path=args.get("file_path"), task_id=task_id)
    with suppress(Exception):
        parsed = json.loads(result)
        if isinstance(parsed, dict) and parsed.get("success"):
            _record_skill_view(dedup_task_id, name, args.get("file_path"), parsed)
            if resolved := parsed.get("name") or name:  # qualified forms return the canonical name
                from tools.skill_usage import bump_use, bump_view
                bump_view(str(resolved))
                # Viewing is actively loading the skill to act on it — that counts as use
                # (the curator's stale timer keys off last_used_at).
                bump_use(str(resolved), task_id=kw.get("task_id"), session_id=kw.get("session_id"))
    return result


registry.register(
    name="skill_view", toolset="skills", schema=SKILL_VIEW_SCHEMA, handler=_skill_view_with_bump,
    check_fn=check_skills_requirements, emoji="📚")


def _protected_skill_path_allowed(task_id: str | None, path: Path) -> bool | None:
    """Return None for ordinary calls, otherwise whether skill tools may read path."""

    authority = _protected_skill_authority(task_id)
    if authority is None:
        return None
    if authority is False:
        return False
    backing_registry = getattr(authority, "backing_registry", None)
    if backing_registry is None:
        return False
    try:
        candidate = path.resolve(strict=True)
    except OSError:
        return False
    for grant in authority.invocation_scope.visible_objects:
        if grant.backing.kind != "host_path":
            continue
        record = backing_registry.get(grant.backing.object_id)
        if (
            record is None
            or not record.exists
            or record.root_symlink
            or record.backing != grant.backing
            or record.object_type != grant.object_type
        ):
            continue
        root = Path(record.backing.identity)
        if (
            candidate == root
            or (grant.object_type == "directory" and root in candidate.parents)
        ):
            # Re-check after candidate resolution. Trusted host records pin and
            # verify the object device/inode on every registry lookup.
            return backing_registry.get(grant.backing.object_id) == record

    scope = authority.invocation_scope
    skill_name = _trusted_skill_name_for_path(candidate)
    if skill_name is None:
        return False
    if bool(getattr(scope, "all_skills", False)):
        return True
    return skill_name in frozenset(getattr(scope, "skill_names", ()))


def _protected_skill_search_dirs(
    task_id: str | None,
    candidates: List[Path],
) -> List[Path] | None:
    if not task_id:
        return None
    authority = _protected_skill_authority(task_id)
    if authority is None:
        return None
    if authority is False:
        return []
    # Candidate roots are trusted host configuration. Authorization is applied
    # to each discovered SKILL.md/path, which supports both selected skills and
    # exact-file visible-object grants without revealing the whole root.
    return list(candidates)


def _discover_skill_linked_files(
    skill_dir: Path,
    *,
    task_id: str | None,
    protected: bool,
) -> tuple[bool, Dict[str, List[str]]]:
    """Discover linked files, revalidating protected authority around scans."""

    directory_authorized = True
    if protected:
        directory_authorized = (
            _protected_skill_path_allowed(task_id, skill_dir) is True
        )
        if not directory_authorized:
            # An exact visible-object grant for SKILL.md authorizes the skill
            # document itself, but must not imply access to sibling resources.
            if _protected_skill_path_allowed(task_id, skill_dir / "SKILL.md") is True:
                return True, {}
            return False, {}

    linked: Dict[str, List[str]] = {}
    specs = (
        ("references", ("*.md",), False),
        ("templates", ("*.md", "*.py", "*.yaml", "*.yml", "*.json", "*.tex", "*.sh"), True),
        ("assets", ("*",), True),
        ("scripts", ("*.py", "*.sh", "*.bash", "*.js", "*.ts", "*.rb"), False),
    )
    for directory_name, patterns, recursive in specs:
        directory = skill_dir / directory_name
        if protected:
            if _protected_skill_path_allowed(task_id, directory) is not True:
                continue
        elif not directory.exists():
            continue

        files: List[str] = []
        for pattern in patterns:
            iterator = directory.rglob(pattern) if recursive else directory.glob(pattern)
            for candidate in iterator:
                if directory_name == "assets" and not candidate.is_file():
                    continue
                if (
                    protected
                    and _protected_skill_path_allowed(task_id, candidate) is not True
                ):
                    continue
                files.append(str(candidate.relative_to(skill_dir)))
        if files:
            linked[directory_name] = files
        if protected and _protected_skill_path_allowed(task_id, skill_dir) is not True:
            return False, {}

    if protected and _protected_skill_path_allowed(task_id, skill_dir) is not True:
        return False, {}
    return True, linked


def _protected_skill_authority(task_id: str | None):
    """Return None for ordinary calls, otherwise the active attempt authority."""

    if not task_id:
        return None
    from tools.delegation_scope import attempt_scope_registry

    authority = attempt_scope_registry.get(task_id)
    if authority is None:
        return None
    if getattr(authority, "state", None) not in {"starting", "active"}:
        return False
    return authority


def _trusted_skill_name_for_path(path: Path) -> str | None:
    """Return the owning active-profile skill name for a trusted skill path."""

    from agent.skill_utils import get_external_skills_dirs

    try:
        candidate = path.resolve(strict=True)
    except OSError:
        return None

    roots = [_skills_dir(), *get_external_skills_dirs()]
    for root in roots:
        try:
            resolved_root = root.resolve(strict=True)
        except OSError:
            continue
        if candidate != resolved_root and resolved_root not in candidate.parents:
            continue

        current = candidate if candidate.is_dir() else candidate.parent
        while current == resolved_root or resolved_root in current.parents:
            skill_md = current / "SKILL.md"
            if skill_md.is_file():
                try:
                    frontmatter, _ = _parse_frontmatter(
                        skill_md.read_text(encoding="utf-8")[:4000]
                    )
                except Exception:
                    return None
                return str(frontmatter.get("name") or current.name)[:MAX_NAME_LENGTH]
            if current == resolved_root:
                break
            current = current.parent
    return None


def _effective_skills(
    *,
    task_id: str | None = None,
    candidate_dirs: List[Path] | None = None,
) -> List[Dict[str, Any]]:
    """Return the catalog authorized for one ordinary or protected caller."""

    if candidate_dirs is None:
        from agent.skill_utils import get_external_skills_dirs

        candidate_dirs = []
        active_skills_dir = _skills_dir()
        if active_skills_dir.exists():
            candidate_dirs.append(active_skills_dir)
        candidate_dirs.extend(get_external_skills_dirs())
    protected_dirs = _protected_skill_search_dirs(task_id, candidate_dirs)
    return _find_all_skills(
        search_dirs=protected_dirs,
        task_id=task_id if protected_dirs is not None else None,
    )
