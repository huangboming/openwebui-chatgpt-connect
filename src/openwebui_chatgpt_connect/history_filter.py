"""Preserve native Responses reasoning through Open WebUI's message conversion."""

SOURCE = "chatgpt-connect"
REASONING_FORMAT = "openai-responses-v1"


def chatgpt_model(model: dict) -> str | None:
    provider = model.get("openai") or {}
    if provider.get("source") == SOURCE:
        return provider.get("id") or model.get("id")
    return None


def reasoning_item(item: dict, model_id: str) -> dict:
    encrypted = item.get("encrypted_content")
    if item.get("type") != "reasoning" or not encrypted:
        return item
    detail = {
        "type": "reasoning.encrypted",
        "format": REASONING_FORMAT,
        "source": SOURCE,
        "model": model_id,
        "id": item.get("id"),
        "data": encrypted,
        "summary": item.get("summary", []),
    }
    other_details = [
        value for value in item.get("reasoning_details", []) if value.get("source") != SOURCE
    ]
    return {**item, "reasoning_details": [*other_details, detail]}


def replay_messages(message: dict, replay: list[tuple[str | None, dict]]) -> list[dict]:
    calls = message.get("tool_calls") or []
    if not calls:
        return [
            {"role": "assistant", "content": "", "output": [item for _, item in replay]},
            *(
                [message]
                if message.get("content") or calls or message.get("reasoning_details")
                else []
            ),
        ]

    # Native message reconstruction groups reasoning and calls separately. Anchor
    # each reasoning item to its next call instead of moving it before all calls.
    messages = []
    call_ids = {call["id"] for call in calls}
    prefix = [item for anchor, item in replay if anchor is not None and anchor not in call_ids]
    if prefix:
        messages.append({"role": "assistant", "content": "", "output": prefix})
    for index, call in enumerate(calls):
        before = [item for anchor, item in replay if anchor == call["id"]]
        if before:
            messages.append({"role": "assistant", "content": "", "output": before})
        part = {**message, "tool_calls": [call]}
        if index:
            part["content"] = ""
            part.pop("reasoning_details", None)
        messages.append(part)
    trailing = [item for anchor, item in replay if anchor is None]
    if trailing:
        messages.append({"role": "assistant", "content": "", "output": trailing})
    return messages


class Filter:
    def stream(self, event: dict, __model__: dict) -> dict:
        model_id = chatgpt_model(__model__)
        if not model_id:
            return event
        kind = event.get("type")
        if kind in {"response.output_item.added", "response.output_item.done"}:
            return {**event, "item": reasoning_item(event["item"], model_id)}
        if kind == "response.completed":
            response = event["response"]
            output = []
            next_call = None
            for item in reversed(response["output"]):
                if item.get("type") == "function_call":
                    next_call = item.get("call_id")
                encoded = reasoning_item(item, model_id)
                if encoded is not item:
                    encoded["reasoning_details"][-1]["before_call_id"] = next_call
                output.append(encoded)
            return {**event, "response": {**response, "output": list(reversed(output))}}
        return event

    def request(self, body: dict, __model__: dict) -> dict:
        model_id = chatgpt_model(__model__)
        if not model_id:
            return body
        messages = []
        for message in body.get("messages", []):
            details = message.get("reasoning_details") or []
            replay = []
            remaining = []
            for detail in details:
                if detail.get("source") != SOURCE:
                    remaining.append(detail)
                    continue
                if detail.get("model") != model_id:
                    continue
                if (
                    detail.get("type") != "reasoning.encrypted"
                    or detail.get("format") != REASONING_FORMAT
                    or not isinstance(detail.get("data"), str)
                    or not detail["data"]
                    or not isinstance(detail.get("summary"), list)
                ):
                    raise ValueError("The saved ChatGPT reasoning context is invalid.")
                item = {
                    "type": "reasoning",
                    "summary": detail["summary"],
                    "encrypted_content": detail["data"],
                }
                if detail.get("id"):
                    item["id"] = detail["id"]
                replay.append((detail.get("before_call_id", ""), item))
            if len(remaining) != len(details):
                message = {**message}
                message.pop("reasoning_details", None)
                if remaining:
                    message["reasoning_details"] = remaining
            if replay:
                messages.extend(replay_messages(message, replay))
            else:
                messages.append(message)
        return {**body, "messages": messages}
