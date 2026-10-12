"""Preserve native history, supply original documents, and route hosted web access."""

import asyncio
import base64
from pathlib import Path

from open_webui.models import files as file_models
from open_webui.models.config import Config
from open_webui.models.users import Users
from open_webui.storage import provider as storage
from open_webui.utils.access_control import files as file_access
from open_webui.utils.access_control import has_permission

from .file_inputs import FILE_LIMIT_ERROR, MAX_FILE_BYTES, document_mime, file_mime

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


def chat_upload(item: dict) -> bool:
    return (
        item.get("type") == "file" and isinstance(item.get("file"), dict) and not item.get("legacy")
    )


def read_file(path: str, remaining_bytes: int) -> bytes:
    try:
        local_path = Path(storage.Storage.get_file(path))
        with local_path.open("rb") as source:
            data = source.read(remaining_bytes + 1)
    except Exception:
        raise ValueError("An uploaded file is unavailable in Open WebUI storage.") from None
    if len(data) > remaining_bytes:
        raise ValueError(FILE_LIMIT_ERROR)
    if not data:
        raise ValueError("An uploaded file is empty.")
    return data


async def native_web_search(body: dict, model: dict, user) -> dict:
    meta = model.get("info", {}).get("meta", {})
    if not await Config.get("web.search.enable"):
        raise ValueError("Enable Web Search in Open WebUI's admin settings first.")
    if not (meta.get("capabilities") or {}).get("web_search", True) or not (
        meta.get("builtinTools") or {}
    ).get("web_search", True):
        raise ValueError("Web search is disabled for this Open WebUI model.")
    if user.role != "admin" and not await has_permission(
        user.id, "features.web_search", await Config.get("user.permissions")
    ):
        raise ValueError("Web search is not permitted for this Open WebUI user.")
    tools = body.get("tools") or []
    if not isinstance(tools, list):
        raise ValueError("tools must be an array.")
    if not any(isinstance(tool, dict) and tool.get("type") == "web_search" for tool in tools):
        tools = [*tools, {"type": "web_search"}]
    # Consume the host switch before its RAG and builtin search paths. Other
    # features and ordinary function tools remain under Open WebUI's control.
    return {
        **body,
        "features": {**(body.get("features") or {}), "web_search": False},
        "tools": tools,
    }


class Filter:
    async def inlet(self, body: dict, __model__: dict, __user__: dict | None = None) -> dict:
        if not chatgpt_model(__model__):
            return body
        web_search = (body.get("features") or {}).get("web_search", False) or any(
            isinstance(tool, dict) and tool.get("type") == "web_search"
            for tool in (body.get("tools") or [])
        )
        files = body.get("files") or []
        if not web_search and not any(chat_upload(item) for item in files):
            return body

        user = await Users.get_user_by_id((__user__ or {}).get("id"))
        if user is None:
            raise ValueError("File inputs and web search require an authenticated Open WebUI user.")
        if web_search:
            body = await native_web_search(body, __model__, user)
        if not any(chat_upload(item) for item in files):
            return body
        messages = body.get("messages") or []
        user_index = next(
            (
                index
                for index in reversed(range(len(messages)))
                if messages[index].get("role") == "user"
            ),
            None,
        )
        if user_index is None:
            raise ValueError("File inputs require a user message.")

        parts = []
        remaining = []
        converted = set()
        used_bytes = 0
        for wrapper in files:
            if not chat_upload(wrapper):
                remaining.append(wrapper)
                continue
            file_id = wrapper.get("id") or wrapper["file"].get("id")
            if not isinstance(file_id, str) or not file_id:
                raise ValueError("Uploaded files must include an Open WebUI file ID.")
            if file_id in converted:
                continue
            file = await file_models.Files.get_file_by_id(file_id)
            if file is None or not (
                file.user_id == user.id
                or user.role == "admin"
                or await file_access.has_access_to_file(file_id, "read", user)
            ):
                raise ValueError("An uploaded file is missing or not accessible.")
            meta = file_models.FileMeta.model_validate(dict(file.meta or {}))
            filename = meta.name or file.filename
            mime = file_mime(filename, meta.content_type)
            if mime.startswith("image/"):
                remaining.append(wrapper)
                continue
            mime = document_mime(mime)
            size = meta.size
            if size is not None and size > MAX_FILE_BYTES - used_bytes:
                raise ValueError(FILE_LIMIT_ERROR)
            data = await asyncio.to_thread(read_file, file.path, MAX_FILE_BYTES - used_bytes)
            used_bytes += len(data)
            parts.append(
                {
                    "type": "file",
                    "file": {
                        "filename": filename,
                        "file_data": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
                    },
                }
            )
            converted.add(file_id)
        if not parts:
            return body
        message = messages[user_index]
        content = message.get("content", "")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}] if content else []
        if not isinstance(content, list):
            raise ValueError("Unsupported user message content.")
        messages = [*messages]
        messages[user_index] = {**message, "content": [*parts, *content]}
        # Consume only upload references before RAG; knowledge sources keep the
        # host's normal retrieval path. The saved chat still owns the originals.
        return {**body, "messages": messages, "files": remaining}

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
            web_calls = []
            for item in reversed(response["output"]):
                if item.get("type") == "function_call":
                    next_call = item.get("call_id")
                if item.get("type") == "web_search_call":
                    web_calls.insert(0, {"before_call_id": next_call, "item": item})
                encoded = reasoning_item(item, model_id)
                if encoded is not item:
                    detail = encoded["reasoning_details"][-1]
                    detail["before_call_id"] = next_call
                    if web_calls:
                        # Native history drops hosted actions. Keep them alongside
                        # their opaque context, without creating synthetic reasoning.
                        detail["web_search_calls"] = web_calls
                        web_calls = []
                output.append(encoded)
            if web_calls:
                for item in reversed(output):
                    if item.get("type") == "reasoning" and item.get("encrypted_content"):
                        detail = item["reasoning_details"][-1]
                        detail["web_search_calls"] = [
                            {**call, "before_reasoning": True} for call in web_calls
                        ] + detail.get("web_search_calls", [])
                        break
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
                    or not isinstance(detail.get("web_search_calls", []), list)
                ):
                    raise ValueError("The saved ChatGPT reasoning context is invalid.")
                item = {
                    "type": "reasoning",
                    "summary": detail["summary"],
                    "encrypted_content": detail["data"],
                }
                if detail.get("id"):
                    item["id"] = detail["id"]
                before, after = [], []
                for call in detail.get("web_search_calls", []):
                    if (
                        not isinstance(call, dict)
                        or not isinstance(call.get("item"), dict)
                        or call["item"].get("type") != "web_search_call"
                    ):
                        raise ValueError("The saved ChatGPT web search history is invalid.")
                    target = before if call.get("before_reasoning") else after
                    target.append((call.get("before_call_id"), call["item"]))
                replay.extend([*before, (detail.get("before_call_id", ""), item), *after])
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
