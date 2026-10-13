"""Read-only task contract boundary for execution/delivery consumers.

No provider is feature-off. A registered reader returning no result or failing
is degraded protection, never a successful empty contract. No task policy,
relationship classifier or writable task store lives at this boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class TaskContractUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskContract:
    id: str
    status: str
    raw_primary_text: str
    raw_supplements: list[str] = field(default_factory=list)
    session_lineage: list[str] = field(default_factory=list)

    @property
    def task_contract(self):
        # Read-only compatibility shape for the existing guard/snapshot consumers.
        return self


def clamp_raw_text(text: str, max_chars: int) -> str:
    """Return exact text or exact prefix + ``…`` + exact suffix.

    This is for bounded judge/summary views only. Persisted canonical text is
    never passed through this helper.
    """
    raw = str(text or "")
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return ""
    if len(raw) <= limit:
        return raw
    if limit == 1:
        return "…"
    head = limit // 2
    tail = limit - head - 1
    return raw[:head] + "…" + (raw[-tail:] if tail else "")


def load_task_contract(session_id: str, *, db: Any = None, hermes_home=None):
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.plugins import has_hook, invoke_hook
    path = getattr(db, "db_path", None)
    home = Path(hermes_home or (Path(path).parent if isinstance(path, (str, Path)) else get_hermes_home()))
    token = set_hermes_home_override(home)
    try:
        if not has_hook("read_task_contract"):
            return None
        results = invoke_hook("read_task_contract", session_id=session_id,
                              session_db=db, hermes_home=str(home))
    finally:
        reset_hermes_home_override(token)
    # Disabled callbacks explicitly return a status instead of going missing;
    # swallowed callback exceptions therefore remain observable here.
    if not results:
        raise TaskContractUnavailable("Registered task-contract provider returned no result")
    active = [r for r in results if not isinstance(r, dict) or r.get("status") != "disabled"]
    if not active:
        return None
    if len(active) != 1:
        raise TaskContractUnavailable("Ambiguous task-contract providers")
    result = active[0]
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise TaskContractUnavailable("Registered task-contract provider failed")
    value = result.get("contract")
    if value is None:
        return None
    if not isinstance(value, dict) or not all(isinstance(value.get(k), str) for k in ("id", "status", "raw_primary_text")):
        raise TaskContractUnavailable("Malformed task contract")
    supplements = value.get("raw_supplements", [])
    lineage = value.get("session_lineage", [])
    if any(not isinstance(items, list) or any(not isinstance(s, str) for s in items) for items in (supplements, lineage)):
        raise TaskContractUnavailable("Malformed task contract list fields")
    return TaskContract(value["id"], value["status"], value["raw_primary_text"], list(supplements), list(lineage))
