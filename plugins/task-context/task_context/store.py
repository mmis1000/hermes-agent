"""Profile-local SQLite authority. No connection/lock lives on an engine instance.

Inputs are admitted atomically, then drained in arrival order under a per-session
OS lock. Classifiers run outside SQL transactions. Publication uses a version CAS
as a second guard; a stale calculation is re-evaluated, never silently dropped.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .policy import RawTaskMessage, TaskContract, TaskIntentState, TaskRelationshipDecision


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(self, hermes_home, *, read_only=False):
        if not hermes_home:
            raise ValueError("explicit Hermes home required")
        self.root = Path(hermes_home).expanduser().resolve() / "task-context"
        self.path = self.root / "tasks.sqlite3"
        self.read_only = read_only
        if read_only:
            return
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create privately before SQLite opens it (do not change process umask).
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS conversations (
                    session_id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0,
                    current_id TEXT, lineage TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS tasks (
                    session_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, state TEXT NOT NULL,
                    PRIMARY KEY(session_id, task_id));
                CREATE TABLE IF NOT EXISTS inputs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    occurrence_id TEXT NOT NULL, payload TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0, decision TEXT,
                    task_version INTEGER, policy_signature TEXT,
                    UNIQUE(session_id, occurrence_id));
                CREATE TABLE IF NOT EXISTS links (
                    session_id TEXT NOT NULL, occurrence_id TEXT NOT NULL,
                    message_uid TEXT NOT NULL,
                    PRIMARY KEY(session_id, occurrence_id, message_uid));
                CREATE TABLE IF NOT EXISTS imports (
                    session_id TEXT PRIMARY KEY, source_digest TEXT NOT NULL,
                    imported_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS aliases (
                    session_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS lineage_receipts (
                    session_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS anchors (
                    session_id TEXT PRIMARY KEY, through_occurrence_id TEXT, states TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS branch_boundaries (
                    session_id TEXT PRIMARY KEY, parent_owner TEXT NOT NULL,
                    through_occurrence_id TEXT, through_seq INTEGER,
                    source TEXT NOT NULL, witnesses TEXT NOT NULL);
            ''')

    @contextmanager
    def connection(self, *, write=False):
        if write and self.read_only:
            raise ValueError("read-only task store")
        target = self.path.resolve().as_uri() + "?mode=ro" if self.read_only else self.path
        db = sqlite3.connect(target, uri=self.read_only, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=30000")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _owner(db, session_id):
        row = db.execute("SELECT owner_id FROM aliases WHERE session_id=?", (session_id,)).fetchone()
        return row[0] if row else session_id

    def owner(self, session_id):
        with self.connection() as db:
            return self._owner(db, session_id)

    @contextmanager
    def serialized(self, session_id):
        """Compression aliases share one lock/authority; branches get a new one."""
        session_id = self.owner(session_id)
        name = hashlib.sha256(session_id.encode()).hexdigest() + ".lock"
        fd = os.open(self.root / name, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _ensure(db, session_id):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id required")
        db.execute("INSERT OR IGNORE INTO conversations(session_id) VALUES(?)", (session_id,))

    def admit(self, session_id, records, message_uid=""):
        with self.connection(write=True) as db:
            session_id = self._owner(db, session_id)
            self._ensure(db, session_id)
            for record in records:
                occurrence = record["occurrence_id"]
                raw = dump(record)
                old = db.execute("SELECT payload FROM inputs WHERE session_id=? AND occurrence_id=?",
                                 (session_id, occurrence)).fetchone()
                if old:
                    # Host row identity may be linked later. Accepted raw authority cannot change.
                    previous = json.loads(old["payload"])
                    for key in ("text", "platform_text", "source_kind", "source_id", "platform"):
                        if previous.get(key) != record.get(key):
                            raise ValueError("accepted input identity reused with different provenance")
                else:
                    db.execute("INSERT INTO inputs(session_id,occurrence_id,payload) VALUES(?,?,?)",
                               (session_id, occurrence, raw))
                if message_uid:
                    db.execute("INSERT OR IGNORE INTO links VALUES(?,?,?)",
                               (session_id, occurrence, message_uid))

    def link(self, session_id, records, message_uid):
        if not message_uid:
            return
        with self.connection(write=True) as db:
            session_id = self._owner(db, session_id)
            for record in records:
                occurrence = record["occurrence_id"]
                if db.execute("SELECT 1 FROM inputs WHERE session_id=? AND occurrence_id=?",
                              (session_id, occurrence)).fetchone():
                    db.execute("INSERT OR IGNORE INTO links VALUES(?,?,?)",
                               (session_id, occurrence, message_uid))

    def _snapshot_in(self, db, session_id):
        session_id = self._owner(db, session_id)
        row = db.execute("SELECT * FROM conversations WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            return 0, [], []
        states = [TaskIntentState.from_json(r[0]) for r in db.execute(
            "SELECT state FROM tasks WHERE session_id=? ORDER BY ordinal", (session_id,))]
        return row["version"], states, json.loads(row["lineage"])

    def _ensure_anchor_in(self, db, session_id):
        """Retain the pre-journal state once, including imported legacy work."""
        if db.execute("SELECT 1 FROM anchors WHERE session_id=?", (session_id,)).fetchone():
            return
        states = self._snapshot_in(db, session_id)[1]
        last = db.execute("SELECT occurrence_id FROM inputs WHERE session_id=? AND processed=1 ORDER BY seq DESC LIMIT 1",
                          (session_id,)).fetchone()
        db.execute("INSERT INTO anchors VALUES(?,?,?)",
                   (session_id, last[0] if last else None, dump([json.loads(s.to_json()) for s in states])))

    def snapshot(self, session_id):
        with self.connection() as db:
            db.execute("BEGIN")  # coherent version + task snapshot, never held for classification
            return self._snapshot_in(db, session_id)

    @staticmethod
    def current(states):
        active = [s for s in states if s.status == "active"]
        return active[-1] if active else (states[-1] if states else None)

    def contract(self, session_id):
        return self._contract_view(*self.snapshot(session_id))

    def _contract_view(self, version, states, lineage):
        state = self.current(states)
        if state is None:
            return None
        other = [s for s in states if s.status == "active" and s.id != state.id]
        # The legacy DTO has one identity. Retain unrelated unresolved tasks as
        # additional literal obligations rather than silently replacing them.
        supplements = list(state.task_contract.raw_supplements)
        for previous in other:
            supplements.extend([previous.task_contract.raw_primary_text, *previous.task_contract.raw_supplements])
        return {
            "id": state.id, "status": state.status,
            "raw_primary_text": state.task_contract.raw_primary_text,
            "raw_supplements": supplements,
            "session_lineage": list(dict.fromkeys([*lineage, *state.session_lineage])),
            "version": version,
            "additional_active_task_ids": [s.id for s in other],
        }

    def pending(self, session_id):
        with self.connection() as db:
            session_id = self._owner(db, session_id)
            row = db.execute("SELECT * FROM inputs WHERE session_id=? AND processed=0 ORDER BY seq LIMIT 1",
                             (session_id,)).fetchone()
            return dict(row) if row else None

    def drain(self, session_id, classify, signature=""):
        """classify(state, record) must not write the store; timeout handled by caller."""
        with self.serialized(session_id):
            while (pending := self.pending(session_id)) is not None:
                record = json.loads(pending["payload"])
                version, states, lineage = self.snapshot(session_id)
                state = self.current(states)
                if not record["text"]:
                    decision = TaskRelationshipDecision(reason_codes=["no_text_input"])
                elif record["source_kind"] != "human":
                    decision = TaskRelationshipDecision(
                        reason_codes=["non_authoritative_source"],
                        fallback_reason="source cannot mutate canonical task intent")
                elif state is None or state.status != "active":
                    decision = TaskRelationshipDecision(relationship="new_task", state_effect="start_new",
                                                        confidence=1, evidence_quotes=[record["text"]],
                                                        reason_codes=["no_active_task"])
                else:
                    result = classify(copy.deepcopy(state), copy.deepcopy(record))
                    fallback = ""
                    if isinstance(result, TaskRelationshipDecision):
                        fallback = result.fallback_reason
                        result = {**result.raw_payload, "relationship": result.relationship,
                                  "state_effect": result.state_effect, "confidence": result.confidence,
                                  "reason_codes": result.reason_codes, "evidence_quotes": result.evidence_quotes}
                    if result is None:
                        decision = TaskRelationshipDecision(
                            reason_codes=["no_structured_relationship_decision"],
                            fallback_reason="bounded relationship judge supplied no decision")
                    else:
                        decision = TaskRelationshipDecision.from_payload(result, raw_text=record["text"], fallback_reason=fallback)
                # Retry the oldest pending record on CAS conflict, using the fresh task.
                self.publish(session_id, pending, version, states, lineage, decision, signature)

    def _apply_input(self, states, lineage, record, decision, annotation, *, now,
                     new_task_id=None, provenance_override=None):
        """One reducer for live publication and classifier-free branch replay."""
        state = self.current(states)
        before_count = len(states)
        human = record["source_kind"] == "human" and bool(record["text"])
        if human:
            provenance = copy.deepcopy(provenance_override) if provenance_override is not None else RawTaskMessage.create(
                record["text"], source_kind="direct_user", relationship=decision.relationship,
                state_effect=decision.state_effect, source_id=record.get("source_id", ""),
                message_id=record.get("message_id", ""), confidence=decision.confidence, judge_result=annotation)
            provenance.id = record["occurrence_id"]
            if state is None or state.status != "active" or decision.state_effect in {"start_new", "supersede"}:
                previous = state
                if previous and previous.status == "active" and decision.state_effect == "supersede":
                    previous.status = "superseded"
                    previous.updated_at = now
                state = TaskIntentState(
                    id=new_task_id or "task-" + uuid.uuid4().hex[:12], created_at=now, updated_at=now, status="active",
                    task_contract=TaskContract(raw_primary_text=record["text"]), raw_messages=[provenance],
                    relationship_to_active_task=decision.relationship, session_lineage=list(lineage),
                    transition={"relationship": decision.relationship,
                                "previous_task_id": previous.id if previous else None,
                                "previous_raw_primary_text": previous.task_contract.raw_primary_text if previous else None})
                states.append(state)
            else:
                state.raw_messages.append(provenance)
                if decision.state_effect == "append_contract":
                    # Identity, not content, deduplicates: repeated identical human asks remain separate.
                    state.task_contract.raw_supplements.append(record["text"])
                elif decision.state_effect == "cancel":
                    state.status = "cancelled"
                    state.transition = {"relationship": "cancellation", "cancellation_raw_text": record["text"]}
                state.relationship_to_active_task = decision.relationship
                state.updated_at = now
        return states[-1].id if len(states) > before_count else None

    def publish(self, session_id, pending, version, states, lineage, decision, signature):
        record = json.loads(pending["payload"])
        now = time.time()
        annotation = {"relationship": decision.relationship, "state_effect": decision.state_effect,
                      "confidence": decision.confidence, "reason_codes": decision.reason_codes,
                      "evidence_quotes": decision.evidence_quotes, "fallback_reason": decision.fallback_reason,
                      "rejected_rewrite_keys": list(decision.raw_payload.get("_rejected_rewrite_keys") or []),
                      "dropped_judge_keys": list(decision.raw_payload.get("_dropped_judge_keys") or []),
                      "raw_payload": decision.raw_payload}
        annotation["applied_at"] = now
        created = self._apply_input(states, lineage, record, decision, annotation, now=now)
        if created:
            annotation["created_task_id"] = created
        with self.connection(write=True) as db:
            session_id = self._owner(db, session_id)
            row = db.execute("SELECT version FROM conversations WHERE session_id=?", (session_id,)).fetchone()
            if row is None or row[0] != version:
                return False
            # Only the oldest unprocessed input can publish. Even callers bypassing
            # the advisory lock cannot overtake or apply one input twice.
            first = db.execute("SELECT seq FROM inputs WHERE session_id=? AND processed=0 ORDER BY seq LIMIT 1",
                               (session_id,)).fetchone()
            if not first or first[0] != pending["seq"]:
                return False
            self._ensure_anchor_in(db, session_id)
            for ordinal, item in enumerate(states):
                db.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?,?)",
                           (session_id, item.id, ordinal, item.to_json()))
            current = self.current(states)
            db.execute("UPDATE conversations SET version=version+1,current_id=? WHERE session_id=? AND version=?",
                       (current.id if current else None, session_id, version))
            db.execute("UPDATE inputs SET processed=1,decision=?,task_version=?,policy_signature=? WHERE seq=?",
                       (dump(annotation), version, signature, pending["seq"]))
            return True

    def _branch_snapshot_in(self, db, owner, identity, lineage):
        """Replay the complete input PREFIX ending at a copied identity.

        Filtering only visible rows would lose compressed-away obligations.
        The reducer consumes published decisions, never a classifier, and reuses
        original task/provenance IDs. The legacy import is a retained anchor.
        """
        version, current_states, _ = self._snapshot_in(db, owner)
        rows = [dict(r) for r in db.execute("SELECT * FROM inputs WHERE session_id=? ORDER BY seq", (owner,))]
        occurrence_ids = set((identity or {}).get("occurrence_ids", []))
        message_uids = set((identity or {}).get("message_uids", []))
        uid_occurrences = {}
        all_links = list(db.execute("SELECT occurrence_id,message_uid FROM links WHERE session_id=?", (owner,)))
        for occurrence, uid in all_links:
            if uid in message_uids:
                uid_occurrences.setdefault(uid, set()).add(occurrence)
        if any(len(values) > 1 for values in uid_occurrences.values()):
            # A UID can survive a later merged-row rewrite. Without the copied
            # constituent IDs it cannot distinguish a batch from a later merge.
            raise ValueError("copied message UID spans ambiguous input occurrences")
        linked = {occurrence: uid for uid, values in uid_occurrences.items() for occurrence in values}
        matched = [r for r in rows if r["occurrence_id"] in occurrence_ids or r["occurrence_id"] in linked]
        witnesses = {"occurrence_ids": sorted(r["occurrence_id"] for r in matched if r["occurrence_id"] in occurrence_ids),
                     "message_uids": sorted(set(linked.values()))}
        anchor = db.execute("SELECT * FROM anchors WHERE session_id=?", (owner,)).fetchone()
        if matched:
            cutoff = matched[-1]["seq"]
            prefix = [r for r in rows if r["seq"] <= cutoff]
            source = "+".join(name for name, values in (
                ("copied_occurrence_ids", witnesses["occurrence_ids"]),
                ("copied_message_uids", witnesses["message_uids"])) if values)
        elif identity is not None and (occurrence_ids or message_uids) and rows:
            # Narrow migration case: a UID-only prefix precedes every linked
            # human journal input. Only the retained, nonempty pre-journal
            # legacy anchor is available, NOT a precise pre-cutover snapshot.
            # Explicit occurrences, pending or unlinked inputs are not negative
            # evidence and must keep failing closed.
            human_ids = {r["occurrence_id"] for r in rows
                         if json.loads(r["payload"])["source_kind"] == "human"}
            linked_ids = {occurrence for occurrence, uid in all_links if uid}
            anchor_states = json.loads(anchor["states"]) if anchor else []
            if (not occurrence_ids and message_uids and human_ids
                    and human_ids <= linked_ids
                    and all(r["processed"] and r["decision"] for r in rows)
                    and anchor is not None and anchor["through_occurrence_id"] is None
                    and anchor_states):
                witnesses = {"occurrence_ids": [], "message_uids": sorted(message_uids),
                             "excluded_human_occurrence_ids": sorted(human_ids),
                             "excluded_journal_message_uids": sorted({uid for occurrence, uid in all_links
                                                                      if occurrence in human_ids and uid})}
                states = [TaskIntentState.from_json(dump(item)) for item in anchor_states]
                return states, rows[0]["task_version"], [], "pre_journal_anchor", witnesses, anchor
            raise ValueError("branch identities have no canonical input boundary")
        else:
            # Pre-cutover/empty histories may have no usable identity. Retain,
            # rather than silently narrow, and persist the ambiguity explicitly.
            prefix = rows
            source = "explicit_current_boundary" if identity is None else "no_identity_conservative"
            cutoff = rows[-1]["seq"] if rows else None
        if any(not r["processed"] or not r["decision"] for r in prefix):
            raise ValueError("branch boundary contains unpublished input decisions")
        if not matched:
            return current_states, version, prefix, source, witnesses, anchor
        if anchor is None:
            raise ValueError("branch history lacks a reconstructable initial anchor")
        through = anchor["through_occurrence_id"]
        anchor_seq = next((r["seq"] for r in rows if r["occurrence_id"] == through), None) if through else 0
        if anchor_seq is None or anchor_seq > cutoff:
            raise ValueError("branch boundary predates available canonical anchor")
        states = [TaskIntentState.from_json(dump(item)) for item in json.loads(anchor["states"])]
        original_provenance = {m.id: m for state in current_states for m in state.raw_messages}
        original_creators = {state.raw_messages[0].id: state for state in current_states if state.raw_messages}
        for item in prefix:
            if item["seq"] <= anchor_seq:
                continue
            record, annotation = json.loads(item["payload"]), json.loads(item["decision"])
            decision = TaskRelationshipDecision(
                relationship=annotation["relationship"], state_effect=annotation["state_effect"],
                confidence=annotation["confidence"], reason_codes=annotation["reason_codes"],
                evidence_quotes=annotation["evidence_quotes"], raw_payload=annotation.get("raw_payload", {}),
                fallback_reason=annotation.get("fallback_reason", ""))
            provenance = original_provenance.get(record["occurrence_id"])
            creator = original_creators.get(record["occurrence_id"])
            task_id = annotation.get("created_task_id") or (creator.id if creator else None)
            state = self.current(states)
            human = record["source_kind"] == "human" and bool(record["text"])
            creates = human and (state is None or state.status != "active" or decision.state_effect in {"start_new", "supersede"})
            if human and (provenance is None or (creates and not task_id)):
                raise ValueError("branch history lacks original task/provenance identity")
            applied_at = annotation.get("applied_at", creator.created_at if creator else provenance.created_at if provenance else 0)
            self._apply_input(states, lineage, record, decision, annotation, now=applied_at,
                              new_task_id=task_id, provenance_override=provenance)
        version = prefix[-1]["task_version"] + 1 if prefix else version
        return states, version, prefix, source, witnesses, anchor

    def inherit(self, parent_id, session_id, reason, *, branch_identity=None):
        if parent_id == session_id or reason not in {"compression", "branch"}:
            return False
        with self.connection(write=True) as db:
            if db.execute("SELECT 1 FROM aliases WHERE session_id=?", (session_id,)).fetchone():
                return False
            if db.execute("SELECT 1 FROM conversations WHERE session_id=?", (session_id,)).fetchone():
                return False
            owner = self._owner(db, parent_id)
            parent = db.execute("SELECT * FROM conversations WHERE session_id=?", (owner,)).fetchone()
            if parent is None:
                return False
            lineage = list(dict.fromkeys([*json.loads(parent["lineage"]), parent_id]))
            if reason == "compression":
                # Rotation is the SAME conversation authority, not a copied
                # snapshot. A judge already running on the parent can publish
                # safely after rotation; its stale version triggers a retry.
                db.execute("INSERT INTO aliases VALUES(?,?)", (session_id, owner))
                db.execute("UPDATE conversations SET lineage=?,version=version+1 WHERE session_id=?",
                           (dump(lineage), owner))
                for row in db.execute("SELECT task_id,state FROM tasks WHERE session_id=?", (owner,)).fetchall():
                    state = TaskIntentState.from_json(row["state"])
                    state.session_lineage = list(dict.fromkeys([*state.session_lineage, *lineage]))
                    db.execute("UPDATE tasks SET state=? WHERE session_id=? AND task_id=?",
                               (state.to_json(), owner, row["task_id"]))
            else:
                # Resolve the copied frontier before writing anything. Prefix
                # replay keeps old compacted inputs and excludes later parents.
                if parent_id in lineage:
                    lineage = lineage[:lineage.index(parent_id) + 1]
                states, version, prefix, source, witnesses, anchor = self._branch_snapshot_in(
                    db, owner, branch_identity, lineage)
                current = self.current(states)
                db.execute("INSERT INTO conversations VALUES(?,?,?,?)",
                           (session_id, version, current.id if current else None, dump(lineage)))
                for ordinal, state in enumerate(states):
                    state.session_lineage = list(dict.fromkeys([*state.session_lineage, *lineage]))
                    db.execute("INSERT INTO tasks VALUES(?,?,?,?)", (session_id, state.id, ordinal, state.to_json()))
                for row in prefix:
                    db.execute('''INSERT INTO inputs(session_id,occurrence_id,payload,processed,decision,task_version,policy_signature)
                                  VALUES(?,?,?,?,?,?,?)''',
                               (session_id, row["occurrence_id"], row["payload"], row["processed"], row["decision"],
                                row["task_version"], row["policy_signature"]))
                db.execute('''INSERT INTO links SELECT ?,l.occurrence_id,l.message_uid FROM links l
                              JOIN inputs i ON i.session_id=? AND i.occurrence_id=l.occurrence_id WHERE l.session_id=?''',
                           (session_id, session_id, owner))
                if anchor:
                    db.execute("INSERT INTO anchors VALUES(?,?,?)",
                               (session_id, anchor["through_occurrence_id"], anchor["states"]))
                else:
                    self._ensure_anchor_in(db, session_id)
                last = prefix[-1] if prefix else {}
                db.execute("INSERT INTO branch_boundaries VALUES(?,?,?,?,?,?)",
                           (session_id, owner, last.get("occurrence_id"), last.get("seq"), source, dump(witnesses)))
            db.execute("INSERT INTO lineage_receipts VALUES(?,?,?)", (session_id, parent_id, reason))
            return True

    def import_legacy(self, session_id, raw, *, before_receipt=None):
        """Atomic data + receipt. A crash before receipt rolls the import back."""
        if self.imported(session_id):
            return False
        digest = hashlib.sha256((raw or "").encode()).hexdigest()
        state = None
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict) or not isinstance(parsed.get("id"), str) or not parsed["id"]:
                raise ValueError("legacy task lacks valid identity")
            contract = parsed.get("task_contract")
            if not isinstance(contract, dict) or not isinstance(contract.get("raw_primary_text"), str):
                raise ValueError("legacy task lacks exact raw contract")
            for values in (contract.get("raw_supplements", []), parsed.get("session_lineage", [])):
                if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                    raise ValueError("legacy task has invalid raw text/lineage")
            state = TaskIntentState.from_json(raw)
        with self.connection(write=True) as db:
            session_id = self._owner(db, session_id)
            if db.execute("SELECT 1 FROM imports WHERE session_id=?", (session_id,)).fetchone():
                return False
            existing = db.execute("SELECT 1 FROM tasks WHERE session_id=?", (session_id,)).fetchone()
            self._ensure(db, session_id)
            if state and not existing:
                extension = parsed.get("task_context_export")
                imported_states = [state]
                if isinstance(extension, dict) and extension.get("version") == 1:
                    imported_states = [TaskIntentState.from_json(dump(item)) for item in extension["tasks"]]
                    for item in extension.get("inputs", []):
                        db.execute('''INSERT OR IGNORE INTO inputs(session_id,occurrence_id,payload,processed,
                                      decision,task_version,policy_signature) VALUES(?,?,?,?,?,?,?)''',
                                   (session_id, item["occurrence_id"], item["payload"], item["processed"],
                                    item["decision"], item["task_version"], item["policy_signature"]))
                    for item in extension.get("links", []):
                        db.execute("INSERT OR IGNORE INTO links VALUES(?,?,?)",
                                   (session_id, item["occurrence_id"], item["message_uid"]))
                    anchor = extension.get("anchor")
                    if anchor is not None:
                        if not isinstance(anchor, dict) or not isinstance(anchor.get("states"), list):
                            raise ValueError("invalid exported branch anchor")
                        through = anchor.get("through_occurrence_id")
                        if through is not None and not isinstance(through, str):
                            raise ValueError("invalid exported anchor identity")
                        for initial in anchor["states"]:
                            if (not isinstance(initial, dict) or not isinstance(initial.get("id"), str) or not initial["id"]
                                    or not isinstance(initial.get("task_contract"), dict)
                                    or not isinstance(initial["task_contract"].get("raw_primary_text"), str)):
                                raise ValueError("invalid exported anchor task")
                        if through is not None and not db.execute(
                            "SELECT 1 FROM inputs WHERE session_id=? AND occurrence_id=? AND processed=1", (session_id, through)
                        ).fetchone():
                            raise ValueError("exported branch anchor lacks input receipt")
                        db.execute("INSERT OR REPLACE INTO anchors VALUES(?,?,?)",
                                   (session_id, through, dump(anchor["states"])))
                    boundary = extension.get("branch_boundary")
                    if isinstance(boundary, dict):
                        db.execute("INSERT OR REPLACE INTO branch_boundaries VALUES(?,?,?,?,?,?)",
                                   (session_id, boundary["parent_owner"], boundary["through_occurrence_id"],
                                    boundary["through_seq"], boundary["source"], boundary["witnesses"]))
                for ordinal, item in enumerate(imported_states):
                    db.execute("INSERT INTO tasks VALUES(?,?,?,?)", (session_id, item.id, ordinal, item.to_json()))
                db.execute("UPDATE conversations SET current_id=?,lineage=?,version=version+1 WHERE session_id=?",
                           (state.id, dump(state.session_lineage), session_id))
            self._ensure_anchor_in(db, session_id)
            if before_receipt:
                before_receipt()
            db.execute("INSERT INTO imports VALUES(?,?,?)", (session_id, digest, time.time()))
            return bool(state and not existing)

    def imported(self, session_id):
        with self.connection() as db:
            session_id = self._owner(db, session_id)
            return db.execute("SELECT 1 FROM imports WHERE session_id=?", (session_id,)).fetchone() is not None

    def export_legacy(self, session_id):
        """Rollback-compatible DTO plus lossless extension from ONE read snapshot."""
        with self.connection() as db:
            db.execute("BEGIN")
            session_id = self._owner(db, session_id)
            version, states, lineage = self._snapshot_in(db, session_id)
            state = self.current(states)
            if state is None:
                return None
            contract = self._contract_view(version, states, lineage)
            result = json.loads(state.to_json())
            result["task_contract"] = {"raw_primary_text": contract["raw_primary_text"],
                                       "raw_supplements": contract["raw_supplements"]}
            result["session_lineage"] = contract["session_lineage"]
            anchor = db.execute("SELECT through_occurrence_id,states FROM anchors WHERE session_id=?", (session_id,)).fetchone()
            boundary = db.execute("SELECT * FROM branch_boundaries WHERE session_id=?", (session_id,)).fetchone()
            result["task_context_export"] = {
                "version": 1, "store_version": version,
                "tasks": [json.loads(s.to_json()) for s in states],
                "inputs": [dict(r) for r in db.execute("SELECT * FROM inputs WHERE session_id=? ORDER BY seq", (session_id,))],
                "links": [dict(r) for r in db.execute("SELECT * FROM links WHERE session_id=?", (session_id,))],
                "anchor": {"through_occurrence_id": anchor[0], "states": json.loads(anchor[1])} if anchor else None,
                "branch_boundary": dict(boundary) if boundary else None,
            }
        return dump(result)
