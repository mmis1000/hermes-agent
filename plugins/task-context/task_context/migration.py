"""Read-only legacy import/lineage; rollback export never writes host state."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .store import Store


class LegacyReader:
    def __init__(self, home, session_db=None):
        self.session_db = session_db
        self.path = Path(getattr(session_db, "db_path", Path(home) / "state.db"))

    def _query(self, sql, args, *, all_rows=False):
        if not self.path.is_file():
            return [] if all_rows else None
        # Do not open a writable SessionDB (schema upgrades/flushes are not migration).
        uri = self.path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            cursor = db.execute(sql, args)
            return cursor.fetchall() if all_rows else cursor.fetchone()

    def raw(self, session_id):
        if self.session_db is not None:
            return self.session_db.get_meta("task_intent:" + session_id)
        row = self._query("SELECT value FROM state_meta WHERE key=?", ("task_intent:" + session_id,))
        return row[0] if row else None

    def branch_identity(self, session_id):
        """Only copied durable identities; never infer a cut from text or time."""
        rows = self._query(
            "SELECT message_uid,absorbed_message_uids,display_metadata FROM messages WHERE session_id=? AND active=1",
            (session_id,), all_rows=True)
        occurrences, uids = set(), set()
        for row in rows:
            absorbed = json.loads(row["absorbed_message_uids"]) if row["absorbed_message_uids"] else []
            if not isinstance(absorbed, list) or not all(isinstance(uid, str) for uid in absorbed):
                raise ValueError("invalid copied message identity")
            metadata = json.loads(row["display_metadata"]) if row["display_metadata"] else {}
            if not isinstance(metadata, dict):
                raise ValueError("invalid copied input metadata")
            copied = {item["occurrence_id"] for item in metadata.get("input_provenance", [])
                      if isinstance(item, dict) and isinstance(item.get("occurrence_id"), str) and item["occurrence_id"]}
            if copied:
                # The copied occurrence list is a stronger witness than a UID
                # whose live parent's row may subsequently absorb more inputs.
                occurrences.update(copied)
            else:
                if row["message_uid"]:
                    uids.add(row["message_uid"])
                uids.update(absorbed)
        return {"occurrence_ids": sorted(occurrences), "message_uids": sorted(uids)}

    def session(self, session_id):
        row = self._query("SELECT id,parent_session_id,source,end_reason,model_config FROM sessions WHERE id=?", (session_id,))
        if row is None:
            return None
        result = dict(row)
        config = json.loads(result["model_config"]) if result["model_config"] else {}
        if not isinstance(config, dict):
            raise ValueError("invalid persisted session model_config")
        result["model_config"] = config
        return result


def ensure_import(store, session_id, session_db=None, *, _seen=None):
    """Before every first provider read; failures propagate, not 'no active task'."""
    if store.imported(session_id):
        return
    seen = set() if _seen is None else _seen
    if session_id in seen:
        raise ValueError("cycle in legacy session lineage")
    seen.add(session_id)
    reader = LegacyReader(store.root.parent, session_db)
    raw = reader.raw(session_id)
    if raw:
        store.import_legacy(session_id, raw)
        return
    row = reader.session(session_id)
    if row and row.get("source") not in {"subagent", "cron", "flush", "automation"}:
        parent_id = row.get("parent_session_id")
        parent = reader.session(parent_id) if parent_id else None
        config = row["model_config"]
        # /branch persists _branched_from at creation. Compression descendants
        # inherit model_config, so a marker only describes THIS edge when its
        # value equals the immediate parent. An explicit delegation edge never
        # inherits human ownership, even when its source label matches.
        if parent and parent.get("source") == row.get("source") and config.get("_delegate_from") != parent_id:
            reason = ("branch" if config.get("_branched_from") == parent_id else
                      "compression" if parent.get("end_reason") == "compression" else None)
            if reason:
                ensure_import(store, parent_id, session_db, _seen=seen)
                identity = reader.branch_identity(session_id) if reason == "branch" else None
                store.inherit(parent_id, session_id, reason, branch_identity=identity)
    store.import_legacy(session_id, None)


def read_task_contract(*, session_id, session_db=None, hermes_home=None):
    try:
        if hermes_home is None:
            from hermes_constants import get_hermes_home
            hermes_home = get_hermes_home()
        store = Store(hermes_home)
        ensure_import(store, session_id, session_db)
        return {"status": "ok", "contract": store.contract(session_id)}
    except Exception as exc:
        # No raw messages, paths or credentials in warning payloads.
        return {"status": "error", "error": f"task-context read failed ({type(exc).__name__})"}


def contract_provider(*, session_id, session_db=None, hermes_home=None):
    """Registered callback: disabled is distinct from absence, empty and failure."""
    try:
        if hermes_home is None:
            from hermes_constants import get_hermes_home
            hermes_home = get_hermes_home()
        from .ingress import profile_scope
        from hermes_cli.config import load_config_readonly
        with profile_scope(hermes_home):
            config = load_config_readonly()
        if (config.get("context") or {}).get("engine", "compressor") != "task-context":
            return {"status": "disabled"}
        return read_task_contract(session_id=session_id, session_db=session_db, hermes_home=hermes_home)
    except Exception as exc:
        return {"status": "error", "error": f"task-context provider failed ({type(exc).__name__})"}


def export_legacy(*, hermes_home, session_id):
    store = Store(hermes_home)
    ensure_import(store, session_id)
    return store.export_legacy(session_id)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Export task context in legacy rollback JSON format (stdout only)")
    parser.add_argument("--home", required=True)
    parser.add_argument("--session", required=True)
    args = parser.parse_args()
    print(export_legacy(hermes_home=args.home, session_id=args.session) or "null")


if __name__ == "__main__":
    main()
