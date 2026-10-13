"""Test-only old API adapter: every operation exercises the real plugin store.

Machine evidence is materialized from the separate input table, not inserted into
the human task state. This retains original policy assertions with new storage.
"""
import json
import uuid

from hermes_constants import get_hermes_home
from task_context.policy import RawTaskMessage
from task_context.store import Store


def _get_session_db():
    store = Store(get_hermes_home())
    store.db_path = store.path
    return store


def migrate_task_intent_to_session(old, new):
    return _get_session_db().inherit(old, new, 'compression')


class TaskIntentManager:
    def __init__(self, session_id):
        self.session_id = session_id
        self.store = _get_session_db()

    @property
    def state(self):
        state = self.store.current(self.store.snapshot(self.session_id)[1])
        if state is None:
            return None
        # Materialized evidence view: raw payload + published judge result.
        with self.store.connection() as db:
            events = db.execute('SELECT payload,decision FROM inputs WHERE session_id=? AND processed=1 ORDER BY seq',
                                (self.session_id,)).fetchall()
        records = []
        for row in events:
            payload, annotation = json.loads(row['payload']), json.loads(row['decision'])
            records.append(RawTaskMessage.from_dict(dict(
                id=payload['occurrence_id'], raw_text=payload['text'],
                source_kind=payload['original_source_kind'], source_id=payload['source_id'],
                message_id=payload['message_id'], relationship_to_active_task=annotation['relationship'],
                state_effect=annotation['state_effect'], judge_result=annotation,
                machine_origin=payload.get('machine_origin', ''))))
        state.raw_messages = records
        return state

    def record_direct_message(self, raw, *, source_kind='direct_user', source_id='', message_id='', relationship_decision=None,
                              machine_origin=''):
        payload = dict(occurrence_id=uuid.uuid4().hex, text=raw, source_kind=(
            'human' if source_kind in {'direct_user', 'direct_external_user'} else 'machine'),
            original_source_kind=source_kind, source_id=source_id, platform='test', message_id=message_id,
            machine_origin=machine_origin)
        self.store.admit(self.session_id, [payload])
        self.store.drain(self.session_id, lambda *_: relationship_decision)
        return self.state

    def record_machine_continuation(self, raw, *, origin, message_id=''):
        return self.record_direct_message(raw, source_kind='machine_continuation', message_id=message_id,
                                          machine_origin=origin)
