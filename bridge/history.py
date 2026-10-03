"""Small presentation pages, separate from the desktop's revision-patched state."""
import hashlib
import json
from collections import Counter
from .model import items_array, normalize_item, normalize_state, ordered_turns, text_content

PAGE_SIZE = 12
PREVIEW_SIZE = 1600


def bounded(value, depth=0):
    if isinstance(value, str):
        return value[:PREVIEW_SIZE]
    if depth >= 4:
        return None
    if isinstance(value, dict):
        return {key: bounded(item, depth + 1) for key, item in list(value.items())[:24]}
    if isinstance(value, list):
        return [bounded(item, depth + 1) for item in value[:12]]
    return value


def brief_item(item):
    if item.get("type") == "reasoning":
        row = normalize_item({**item, "content": []})
        row["detailAvailable"] = bool(item.get("content"))
        row["detailVersion"] = detail_version(item)
        return row
    if item.get("type") in ("userMessage", "steeringUserMessage", "agentMessage",
                            "assistantMessage", "contextCompaction", "error"):
        return normalize_item(item)
    row = normalize_item(bounded(item))
    row["id"] = item.get("id")
    row["detailAvailable"] = True
    row["detailVersion"] = detail_version(item)
    return row


def detail_version(item):
    return hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()[:16]


def keyed_items(turn):
    counts = {}
    for item in items_array(turn.get("items", [])):
        identity = (item.get("type"), str(item.get("id")))
        occurrence = counts.get(identity, 0)
        counts[identity] = occurrence + 1
        yield json.dumps([*identity, occurrence], separators=(",", ":")), item


def state_delta(previous, current):
    if previous.get("syncId") != current.get("syncId"):
        return current
    old = {turn["id"]: turn for turn in previous["turns"]}
    changed = []
    for turn in current["turns"]:
        prior = old.get(turn["id"])
        if prior == turn:
            continue
        ids = [message.get("id") for message in turn["messages"]]
        old_ids = [message.get("id") for message in prior["messages"]] if prior else []
        if prior and all(key is not None for key in ids + old_ids) and len(set(ids)) == len(ids) and len(set(old_ids)) == len(old_ids):
            messages = {message["id"]: message for message in prior["messages"]}
            changed.append({**turn, "messagesDelta": True, "messageOrder": ids,
                            "messages": [message for message in turn["messages"]
                                         if messages.get(message["id"]) != message]})
        else:
            changed.append(turn)
    return {**current, "delta": True, "baseSequence": previous["sequence"],
            "turnOrder": [turn["id"] for turn in current["turns"]],
            "turns": changed}


def present_turn(turn, fold=True):
    items = items_array(turn.get("items", []))
    completed = turn.get("status") in ("completed", "interrupted")
    process = False
    if fold and completed:
        answers = [item for item in items if item.get("type") in ("agentMessage", "assistantMessage")]
        final = next((item for item in reversed(answers) if item.get("phase") == "final_answer"),
                     answers[-1] if answers else None)
        visible = [item for item in items if item.get("type") in (
            "userMessage", "steeringUserMessage", "contextCompaction", "error") or item is final]
        process = len(visible) < len(items)
    else:
        visible = items
    value = normalize_state({"turns": [{**turn, "items": []}]})["turns"][0]
    keys = {id(item): key for key, item in keyed_items(turn)}
    duplicate_ids = {key for key, count in Counter(item.get("id") for item in items).items() if count > 1}
    messages = []
    for item in visible:
        row = brief_item(item)
        row["detailKey"] = keys[id(item)]
        if row.get("id") in duplicate_ids:
            row["id"] = row["detailKey"]
        messages.append(row)
    # Native opening prompts can live in turn.params rather than in its items.
    value["messages"] = messages if any(row.get("role") == "user" for row in messages) else value["messages"] + messages
    value["processAvailable"] = process
    return value


def merged_turns(saved, native, prefer_native=True):
    result = list(ordered_turns(saved or {}))
    positions = {turn.get("turnId"): index for index, turn in enumerate(result) if turn.get("turnId")}
    for turn in ordered_turns(native or {}):
        key = turn.get("turnId")
        if key not in positions:
            opening = turn_opening(turn)
            matches = [index for index, row in enumerate(result)
                       if str(row.get("turnId", "")).startswith("saved-") and opening
                       and turn_opening(row) == opening]
            if len(matches) == 1:
                positions[key] = matches[0]
        if key and key in positions:
            saved_turn = result[positions[key]]
            if not prefer_native:
                continue
            native_items = turn.get("items", [])
            if isinstance(native_items, dict) and not native_items.get("isComplete", True):
                native = items_array(native_items)
                ids = {item.get("id") for item in native if item.get("id") is not None}
                missing = [item for item in items_array(saved_turn.get("items", []))
                           if item.get("id") not in ids]
                turn = {**turn, "items": missing + native}
            result[positions[key]] = turn
        else:
            if key:
                positions[key] = len(result)
            result.append(turn)
    return result


def turn_opening(turn):
    return next((text_content(item.get("content", [])) for item in items_array(turn.get("items", []))
                 if item.get("type") == "userMessage"), text_content(turn.get("params", {}).get("input", [])))


def page(turns, before=None):
    end = len(turns)
    if before:
        end = next((index for index, turn in enumerate(turns) if turn.get("turnId") == before), -1)
        if end < 0:
            raise ValueError("历史位置已变化，请刷新线程")
    start = max(0, end - PAGE_SIZE)
    rows = turns[start:end]
    return rows, rows[0].get("turnId") if start and rows else None
