"""Process-local lookup for the live AIAgent serving an authorized session.

The gateway can dispatch registry tools through adapters that do not preserve the
optional ``parent_agent`` keyword.  Stateful tools may use this registry only
after their normal durable session-ownership check has succeeded.
"""

from __future__ import annotations

import threading
import weakref
from typing import Any, Iterable, Optional

_lock = threading.RLock()
_agents: dict[str, weakref.ReferenceType[Any]] = {}


def register_live_agent(agent: Any, *, session_keys: Iterable[str] = ()) -> None:
    """Associate *agent* with non-empty routing/durable session identities."""
    if agent is None:
        return
    keys = {
        str(key).strip()
        for key in (*session_keys, getattr(agent, "session_id", None))
        if isinstance(key, str) and key.strip()
    }
    if not keys:
        return
    try:
        reference = weakref.ref(agent)
    except TypeError:
        return
    with _lock:
        for key in keys:
            _agents[key] = reference


def resolve_live_agent(session_keys: Iterable[str]) -> Optional[Any]:
    """Return the first still-live agent bound to an exact supplied identity."""
    with _lock:
        for raw_key in session_keys:
            key = str(raw_key or "").strip()
            if not key:
                continue
            reference = _agents.get(key)
            agent = reference() if reference is not None else None
            if agent is not None:
                return agent
            if reference is not None:
                _agents.pop(key, None)
    return None


def clear_live_agents_for_tests() -> None:
    with _lock:
        _agents.clear()
