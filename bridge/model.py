"""Normalize desktop state without changing the native thread or provider."""
import copy
import json
import math


def context_usage(info, live=False):
    info = info if isinstance(info, dict) else {}
    last = info.get("last") or info.get("last_token_usage") or {}
    used = last.get("totalTokens", last.get("total_tokens"))
    window = info.get("modelContextWindow", info.get("model_context_window"))
    valid = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if not valid(used) or used < 0 or not valid(window) or window <= 0:
        return {"available": False, "usedTokens": None, "contextWindow": None, "percent": None, "live": False}
    used = min(used, window)
    return {"available": True, "usedTokens": used, "contextWindow": window,
            "remainingTokens": max(window - used, 0), "percent": used / window * 100, "live": live}


def apply_patches(state, patches):
    for patch in patches:
        path = patch.get("path", [])
        if isinstance(path, str):
            path = [p.replace("~1", "/").replace("~0", "~") for p in path.split("/")[1:]]
        if not path:
            if patch["op"] != "replace":
                raise ValueError("Unsupported root patch")
            state = copy.deepcopy(patch["value"])
            continue
        target = state
        for key in path[:-1]:
            target = target[int(key)] if isinstance(target, list) else target[key]
        key = path[-1]
        operation = patch["op"]
        if isinstance(target, list):
            index = len(target) if key == "-" else int(key)
            if operation == "add":
                target.insert(index, copy.deepcopy(patch["value"]))
            elif operation == "remove":
                target.pop(index)
            elif operation == "replace":
                target[index] = copy.deepcopy(patch["value"])
            else:
                raise ValueError("Unsupported list patch")
        elif operation == "remove":
            del target[key]
        elif operation in ("replace", "add"):
            target[key] = copy.deepcopy(patch["value"])
        else:
            raise ValueError("Unsupported patch")
    return state


def ordered_turns(state):
    history = state.get("turnHistory", {})
    if history.get("kind") != "canonical":
        return state.get("turns", [])
    history = history.get("history", {})
    entities = history.get("entitiesByKey", {})
    turns, seen = [], set()
    for island in history.get("islands", []):
        for entry in island.get("entries", []):
            key = entry.get("value")
            if key in entities and key not in seen:
                seen.add(key)
                turns.append(entities[key])
    return turns


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(x.get("text", "")) for x in content if isinstance(x, dict) and x.get("type") in ("text", "input_text", "output_text", "inputText"))
    return ""


def items_array(items):
    if isinstance(items, list):
        return items
    if isinstance(items, dict):
        entities = items.get("entitiesByKey", {})
        if entities:
            return [entities[e["value"]] for island in items.get("islands", []) for e in island.get("entries", []) if e.get("value") in entities]
        return list(items.get("items", []))
    return []


def normalize_item(item):
    kind = item.get("type", "unknown")
    row = {"id": item.get("id"), "kind": kind, "status": item.get("status")}
    if kind in ("userMessage", "steeringUserMessage"):
        if kind == "steeringUserMessage":
            item = {**item, "content": item.get("input", [])}
        text = text_content(item.get("content", []))
        replies = question_replies(text)
        if replies:
            text = "\n\n".join(str(r.get("question", "")) + "\n" + str(r.get("answer", "")) for r in replies)
        row.update(role="user", text=text)
    elif kind in ("agentMessage", "assistantMessage"):
        row.update(role="assistant", text=item.get("text", ""), phase=item.get("phase"))
    elif kind == "reasoning":
        row.update(role="activity", title="思考过程", text=text_blocks(item.get("summary")),
                   summary=text_blocks(item.get("summary")), detail=text_blocks(item.get("content")))
    elif kind == "contextCompaction":
        row.update(role="activity", title="上下文压缩", text="上下文已整理" if item.get("status") != "inProgress" else "正在整理上下文")
    elif kind == "commandExecution":
        row.update(role="activity", title="执行命令", text=item.get("command", ""), output=item.get("aggregatedOutput", ""), exitCode=item.get("exitCode"))
    elif kind == "fileChange":
        row.update(role="activity", title="文件变更", text="\n\n".join(str(c.get("path", "")) + "\n" + str(c.get("diff", "")) for c in item.get("changes", [])))
    elif kind in ("mcpToolCall", "dynamicToolCall"):
        row.update(role="activity", title=" · ".join(str(x) for x in (item.get("server"), item.get("tool", item.get("toolName"))) if x), text=json.dumps(item.get("arguments", {}), ensure_ascii=False, indent=2), output=json.dumps(item.get("result", item.get("contentItems", item.get("error", ""))), ensure_ascii=False, indent=2))
    elif kind == "error":
        row.update(role="error", text=item.get("message", "执行失败"))
    else:
        row.update(role="activity", title=kind, text=json.dumps(item, ensure_ascii=False, indent=2))
    # Preserve non-text attachment descriptors. They are not fetched from arbitrary URLs.
    attachments = [x for x in item.get("content", []) if isinstance(x, dict) and x.get("type") not in ("text", "input_text", "output_text")] if kind in ("userMessage", "steeringUserMessage") and isinstance(item.get("content"), list) else []
    if attachments:
        row["attachments"] = attachments
    return row


def normalize_state(state, connected=True):
    turns = ordered_turns(state)
    result = []
    for ordinal, turn in enumerate(turns):
        items = items_array(turn.get("items", []))
        messages = [normalize_item(item) for item in items]
        if not any(x.get("role") == "user" for x in messages):
            opening = text_content(turn.get("params", {}).get("input", []))
            if opening:
                messages.insert(0, {"id": "opening-" + str(turn.get("turnId", ordinal)), "role": "user", "kind": "userMessage", "text": opening})
        started = finite_number(turn.get("turnStartedAtMs"))
        duration = finite_number(turn.get("durationMs"))
        ended = finite_number(turn.get("completedAtMs"))
        if ended is None and started is not None and duration is not None and turn.get("status") != "inProgress":
            ended = started + duration
        result.append({"id": turn.get("turnId") or str(ordinal), "status": turn.get("status"),
                       "startedAt": started, "completedAt": ended, "durationMs": duration, "messages": messages,
                       "error": turn.get("error"), "diff": turn.get("diff")})
    history = state.get("turnHistory", {}).get("history", {})
    requests = []
    for request in state.get("requests", []):
        normalized = normalize_request(request)
        item_id = request.get("params", {}).get("itemId")
        related = next((item for turn in turns for item in items_array(turn.get("items")) if item.get("id") == item_id), None) if item_id else None
        if related and normalized["supported"]:
            if request.get("method") == "item/fileChange/requestApproval":
                normalized["params"]["changes"] = copy.deepcopy(related.get("changes", []))
            elif request.get("method") == "item/commandExecution/requestApproval" and not normalized["params"].get("command"):
                normalized["params"]["command"] = related.get("command", "")
        requests.append(normalized)
    settings = thread_settings(state)
    return {"id": state.get("id", state.get("sessionId")), "title": state.get("title") or "未命名聊天",
            "cwd": state.get("cwd"), "model": settings["model"], "provider": state.get("modelProvider"), "effort": settings["effort"],
            "connected": connected, "status": state.get("threadRuntimeStatus", {}).get("type", "idle"),
            "contextUsage": context_usage(state.get("latestTokenUsageInfo"), live=connected),
            "turns": result, "requests": requests + async_requests(state),
            "historyComplete": history.get("isComplete", state.get("turnsPagination", {}).get("hasLoadedOldest", True))}


def text_blocks(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "\n\n".join(x if isinstance(x, str) else str(x.get("text") or "") for x in value
                     if isinstance(x, (str, dict)))


def finite_number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def thread_settings(state):
    settings = state.get("latestThreadSettings") or {}
    collaboration = (state.get("latestCollaborationMode") or {}).get("settings") or {}
    return {
        "model": settings.get("model") or collaboration.get("model") or state.get("latestModel"),
        "effort": settings["effort"] if "effort" in settings else
                  collaboration["reasoning_effort"] if "reasoning_effort" in collaboration else
                  state.get("latestReasoningEffort"),
    }


def normalize_request(request):
    result = {"id": request.get("id"), "method": request.get("method"), "params": {}}
    params = request.get("params", {})
    method = request.get("method")
    fields = {
        "item/commandExecution/requestApproval": ("command", "cwd", "reason", "availableDecisions", "networkApprovalContext"),
        "item/fileChange/requestApproval": ("reason", "grantRoot", "changes"),
        "item/permissions/requestApproval": ("reason", "permissions"),
        "item/tool/requestUserInput": ("questions",),
        "tool/requestUserInput": ("questions",),
        "mcpServer/elicitation/request": ("serverName", "message", "mode", "requestedSchema"),
    }
    # Identity verification is intentionally desktop-only, including its challenge data.
    verification = "openai/userVerification" in json.dumps(params.get("_meta", {})) or "openai/userVerification" in params
    result["supported"] = method in fields and not verification
    if method == "mcpServer/elicitation/request" and (params.get("mode", "form") != "form" or not form_supported(params.get("requestedSchema", {}))):
        result["supported"] = False
    if result["supported"]:
        result["params"] = {k: copy.deepcopy(params[k]) for k in fields[method] if k in params}
    else:
        result["params"] = {"message": "请在桌面 App 处理此请求"}
    return result


def form_supported(schema):
    if schema.get("type") != "object" or set(schema) - {"type", "properties", "required", "title", "description", "$schema", "additionalProperties"}:
        return False
    allowed = {"type", "title", "description", "enum", "enumNames", "default", "minLength", "maxLength", "minimum", "maximum"}
    return all(isinstance(field, dict) and not set(field) - allowed and field.get("type", "string") in ("string", "number", "integer", "boolean") for field in schema.get("properties", {}).values())


def question_replies(text):
    prefix, suffix = "<send_user_message_question_reply>", "</send_user_message_question_reply>"
    if not text.strip().startswith(prefix) or not text.strip().endswith(suffix):
        return []
    try:
        value = json.loads(text.strip()[len(prefix):-len(suffix)])
        return value if isinstance(value, list) and all(isinstance(r, dict) for r in value) else []
    except ValueError:
        return []


def async_requests(state):
    result = []
    turns = ordered_turns(state)
    for turn in turns[-1:]:
        if turn.get("status") not in ("inProgress", "completed"):
            continue
        items = items_array(turn.get("items"))
        answered = set()
        for item in items:
            if item.get("type") == "userMessage" or (item.get("type") == "steeringUserMessage" and item.get("status") == "accepted"):
                text = text_content(item.get("content", item.get("input", [])))
                answered.update(r.get("questionItemId") for r in question_replies(text))
        for item in items:
            if item.get("type") != "agentMessage" or not item.get("questions"):
                continue
            questions = []
            for index, question in enumerate(item["questions"]):
                key = json.dumps(["request_user_input_async", item["id"], index], ensure_ascii=False, separators=(",", ":"))
                if key not in answered:
                    questions.append({"id": key, "question": question["title"], "options": [{"label": o} for o in (question.get("options") or [])]})
            if questions:
                result.append({"id": "async:" + str(turn.get("turnId")) + ":" + item["id"], "method": "bridge/requestUserInputAsync",
                               "supported": True, "params": {"questions": questions}})
    return result
