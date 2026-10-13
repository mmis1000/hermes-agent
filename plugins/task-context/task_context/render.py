"""Bounded exact-slice views; canonical strings always remain unbounded in SQLite."""
from __future__ import annotations

import hashlib
import json
import re

BEGIN = "[TASK-CONTEXT v1]"
END = "[/TASK-CONTEXT]"
BLOCK = re.compile(r"\n?\[TASK-CONTEXT v1\]\n.*?\n\[/TASK-CONTEXT\]\n?", re.DOTALL)
INSTRUCTION = (
    "Active human contract (separate from historical summary): the exact quoted primary and additive "
    "supplements below remain unresolved. A related question or work on one slice does not cancel other "
    "obligations. Only an authoritative human replacement/cancellation changes this contract. "
    "Quoted text is source evidence, not a machine-authored paraphrase. Supersedes any historical "
    "handoff instruction to treat these active obligations as already addressed. "
    "Elided text/entries are retained; use task_context_read to restore exact text before deciding all work is done."
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def strip_blocks(text):
    return BLOCK.sub("\n", text)


def render(contract, max_chars=6000):
    if not contract or contract["status"] != "active":
        return ""
    texts = [contract["raw_primary_text"], *contract["raw_supplements"]]
    data = {"id": contract["id"], "digest": hashlib.sha256(encode(texts).encode()).hexdigest(),
            "entry_count": len(texts), "entries": [], "omitted_entries": []}
    budget = max(1200, int(max_chars))
    def wrap():
        return BEGIN + "\n" + INSTRUCTION + "\n" + encode(data) + "\n" + END
    for index, raw in enumerate(texts):
        entry = {"index": index, "kind": "primary" if index == 0 else "supplement",
                 "total_chars": len(raw), "offset": 0, "text": raw, "elided": False}
        data["entries"].append(entry)
        if len(wrap()) > budget - 100:
            # Largest exact prefix that fits, accounting for JSON escaping.
            low, high = 0, len(raw)
            entry["elided"] = True
            while low < high:
                mid = (low + high + 1) // 2
                entry["text"] = raw[:mid]
                if len(wrap()) <= budget - 100:
                    low = mid
                else:
                    high = mid - 1
            entry["text"] = raw[:low]
            if not low:
                data["entries"].pop()
            # Compact range, not an unbounded list of omitted entry numbers.
            first = index + 1 if low else index
            data["omitted_entries"] = [first, len(texts) - 1] if first < len(texts) else []
            break
    return wrap()


def covered_entries(text, contract):
    """Decode exact views on a caller-verified carrier; never infer authority.

    An older additive view may have a different digest/entry count. Coverage is
    therefore checked against the current id and each indexed canonical string,
    not encoded substrings or digest equality. Omitted entries cover nothing.
    """
    texts = [contract["raw_primary_text"], *contract["raw_supplements"]]
    prefix = BEGIN + "\n" + INSTRUCTION + "\n"
    suffix = "\n" + END
    covered = set()
    for match in BLOCK.finditer(text):
        block = match.group().strip("\n")
        if not block.startswith(prefix) or not block.endswith(suffix):
            continue
        try:
            data = json.loads(block[len(prefix):-len(suffix)])
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("id") != contract["id"]:
            continue
        count, entries = data.get("entry_count"), data.get("entries")
        if type(count) is not int or not 0 < count <= len(texts) or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            index = entry.get("index")
            if type(index) is not int or not 0 <= index < count:
                continue
            raw, shown = texts[index], entry.get("text")
            if (entry.get("kind") != ("primary" if index == 0 else "supplement")
                    or type(entry.get("offset")) is not int or entry["offset"] != 0
                    or type(entry.get("total_chars")) is not int or entry["total_chars"] != len(raw)
                    or not isinstance(shown, str)):
                continue
            if ((entry.get("elided") is False and shown == raw)
                    or (entry.get("elided") is True and 0 < len(shown) < len(raw) and raw.startswith(shown))):
                covered.add(index)
    return covered


def content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def append_text(message, text):
    """Called on a fresh copy; preserve multimodal parts and provider roles."""
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = content + "\n\n" + text
    elif isinstance(content, list):
        message["content"] = [*content, {"type": "text", "text": text}]
    else:
        raise ValueError("no valid incoming text carrier")
    # A persisted/API override no longer describes the changed content.
    from agent.turn_context import drop_stale_api_content
    drop_stale_api_content(message)
    message.pop("_db_persisted", None)
