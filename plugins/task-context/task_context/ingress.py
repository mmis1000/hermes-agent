"""Admitted provenance validation and deadline-bounded auxiliary routing."""
from __future__ import annotations

import contextvars
import queue
import threading
from contextlib import contextmanager

from .judge import TaskIntentMicroJudge, TaskIntentMicroJudgeConfig
from .policy import TaskRelationshipDecision


def admitted_records(message):
    """Never derive human authority or identity from decorated transcript text."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return []
    metadata = message.get("display_metadata")
    records = metadata.get("input_provenance") if isinstance(metadata, dict) else None
    if not isinstance(records, list):
        return []
    result = []
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("invalid input provenance record")
        if not isinstance(item.get("occurrence_id"), str) or not item["occurrence_id"]:
            raise ValueError("accepted input lacks occurrence_id")
        if not isinstance(item.get("text"), str):
            raise ValueError("accepted input lacks exact adapter text")
        if "platform_text" in item and item["platform_text"] is not None and not isinstance(item["platform_text"], str):
            raise ValueError("platform_text must be exact text or absent")
        kind = item.get("source_kind", "unknown")
        if kind not in {"human", "machine", "unknown"}:
            kind = "unknown"
        record = {"occurrence_id": item["occurrence_id"], "text": item["text"], "source_kind": kind}
        for key in ("source_id", "platform", "message_id"):
            record[key] = str(item.get(key) or "")
        if isinstance(item.get("platform_text"), str):
            record["platform_text"] = item["platform_text"]
        result.append(record)
    return result


@contextmanager
def profile_scope(home):
    # Context-local, not os.environ: concurrent profiles cannot redirect each other.
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def load_judge_config(home):
    from hermes_cli.config import load_config_readonly
    with profile_scope(home):
        config = load_config_readonly()
    if not isinstance(config, dict):
        config = {}
    root = config.get("task_intents")
    return TaskIntentMicroJudgeConfig.from_mapping(root.get("relationship_judge") if isinstance(root, dict) else None)


def classify_with_deadline(state, record, *, home, runtime, config, llm_call=None):
    if not config.enabled:
        return None
    # Worker owns no store/engine reference and can only return a result. A timed
    # out call cannot publish a late mutation or populate a shared memoization cache.
    output = queue.Queue(maxsize=1)
    context = contextvars.copy_context()

    def run():
        try:
            with profile_scope(home):
                def call(**kwargs):
                    if llm_call is not None:
                        return llm_call(**kwargs)
                    from agent.auxiliary_client import call_llm
                    return call_llm(task="task_intent", main_runtime=runtime,
                                    temperature=0, **kwargs)
                judge = TaskIntentMicroJudge(llm_call=call, config=config)
                value = judge.judge(state=state, current_message=record["text"],
                                    message_id=record["occurrence_id"], source_kind="direct_user", use_cache=False)
            output.put(value)
        except Exception as exc:
            output.put(TaskRelationshipDecision(reason_codes=["judge_error"],
                       fallback_reason=type(exc).__name__))

    threading.Thread(target=lambda: context.run(run), name="task-context-judge", daemon=True).start()
    try:
        return output.get(timeout=config.timeout_seconds + 0.5)
    except queue.Empty:
        return TaskRelationshipDecision(reason_codes=["judge_timeout"],
                                        fallback_reason="relationship judge deadline exceeded")
