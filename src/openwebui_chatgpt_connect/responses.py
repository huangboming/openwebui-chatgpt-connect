"""Adapt and relay OpenAI model catalog and Responses requests."""

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .auth import ChatGPTAuth
from .settings import ERROR_STATUS, NO_STORE, RESOURCE

STATEFUL_FIELDS = (
    "previous_response_id",
    "conversation",
    "prompt",
    "multi_agent",
    "max_tool_calls",
)
TOKEN_LIMIT_FIELDS = ("max_output_tokens", "max_tokens", "max_completion_tokens")
KNOWN_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
CATALOG_TTL = 300
TOOL_NAMESPACE = "openwebui"


@dataclass(frozen=True)
class CatalogModel:
    id: str
    name: str
    reasoning_efforts: tuple[str, ...]

    def api_model(self) -> dict:
        return {
            "id": self.id,
            "name": self.name if self.name.startswith("ChatGPT/") else f"ChatGPT/{self.name}",
            "object": "model",
            "owned_by": "openai",
            "source": "chatgpt-connect",
        }


@dataclass(frozen=True)
class CatalogSnapshot:
    models: tuple[CatalogModel, ...]
    fetched_at: float

    def find(self, model_id: str | None) -> CatalogModel | None:
        return next((model for model in self.models if model.id == model_id), None)


class CatalogUpstreamError(Exception):
    def __init__(self, response: httpx.Response):
        self.response = response
        super().__init__(f"Model catalog request failed with HTTP {response.status_code}.")


def catalog_snapshot(data: dict) -> CatalogSnapshot:
    raw_models = data["models"]
    if not isinstance(raw_models, list):
        raise ValueError("invalid catalog")
    models = []
    for raw in raw_models:
        if (
            not isinstance(raw, dict)
            or raw.get("visibility") != "list"
            or not isinstance(raw.get("slug"), str)
            or not raw["slug"]
        ):
            continue
        efforts = []
        for option in raw.get("supported_reasoning_levels") or []:
            effort = (
                option
                if isinstance(option, str)
                else option.get("effort")
                if isinstance(option, dict)
                else None
            )
            if isinstance(effort, str) and effort and effort not in efforts:
                efforts.append(effort)
        default = raw.get("default_reasoning_level")
        if not isinstance(default, str) or not default:
            default = None
        if default and default not in efforts:
            efforts.append(default)
        display_name = raw.get("display_name")
        models.append(
            CatalogModel(
                id=raw["slug"],
                name=display_name
                if isinstance(display_name, str) and display_name
                else raw["slug"],
                reasoning_efforts=tuple(efforts),
            )
        )
    return CatalogSnapshot(tuple(models), time.monotonic())


def reasoning_parameters(body: dict) -> dict | None:
    raw = body.get("reasoning")
    if raw is not None and not isinstance(raw, dict):
        raise HTTPException(400, "reasoning must be an object.")
    reasoning = dict(raw or {})
    nested_effort = reasoning.get("effort")
    flat_effort = body.get("reasoning_effort")
    if flat_effort == "":
        flat_effort = None
    for effort in (nested_effort, flat_effort):
        if effort is not None and (not isinstance(effort, str) or not effort):
            raise HTTPException(400, "Reasoning effort must be a non-empty string.")
    if nested_effort and flat_effort and nested_effort != flat_effort:
        raise HTTPException(400, "reasoning.effort and reasoning_effort select different values.")
    effort = nested_effort or flat_effort
    if effort:
        reasoning["effort"] = effort
    else:
        reasoning.pop("effort", None)
    return reasoning if raw is not None or effort else None


def reasoning_payload(reasoning: dict | None, model: CatalogModel | None) -> dict | None:
    supported = model.reasoning_efforts if model else ()
    if reasoning is None and not supported:
        return None
    reasoning = reasoning or {}
    effort = reasoning.get("effort")
    if effort and supported and effort not in supported:
        raise HTTPException(
            400,
            f"Model {model.id} supports reasoning effort: {', '.join(supported)}.",
        )
    if effort and not supported and effort not in KNOWN_REASONING_EFFORTS:
        raise HTTPException(400, f"Unsupported reasoning effort: {effort}.")
    if reasoning.get("summary") is None:
        reasoning["summary"] = "auto"
    return reasoning


def image_part(part: dict) -> dict:
    if part.get("file_id") is not None:
        raise HTTPException(400, "SIWC does not support image file IDs; use image_url.")
    image_url = part.get("image_url")
    if not isinstance(image_url, str) or not image_url:
        raise HTTPException(400, "image_url must be an HTTPS URL or a base64 image data URL.")
    if image_url.startswith("data:"):
        header, _, encoded = image_url.partition(",")
        media_type, _, encoding = header.removeprefix("data:").partition(";")
        if media_type not in ("image/png", "image/jpeg", "image/webp", "image/gif") or (
            encoding != "base64"
        ):
            raise HTTPException(400, "Image data URLs must use base64 PNG, JPEG, WEBP, or GIF.")
        try:
            if not base64.b64decode(encoded, validate=True):
                raise ValueError("empty image")
        except ValueError:
            raise HTTPException(400, "Image data must contain valid, non-empty base64.") from None
    else:
        try:
            url = urlsplit(image_url)
            valid = (
                url.scheme == "https"
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and not any(char.isspace() for char in image_url)
            )
        except ValueError:
            valid = False
        if not valid:
            raise HTTPException(400, "image_url must be an HTTPS URL without credentials.")
    detail = part.get("detail", "auto")
    if detail not in ("auto", "low", "high", "original"):
        raise HTTPException(400, "Image detail must be auto, low, high, or original.")
    return {"type": "input_image", "image_url": image_url, "detail": detail}


def content_parts(content, text_type: str) -> list[dict]:
    if isinstance(content, str):
        return [{"type": text_type, "text": content}]
    if not isinstance(content, list):
        raise HTTPException(400, "Unsupported message content.")
    parts = []
    for part in content:
        if not isinstance(part, dict):
            raise HTTPException(400, "Unsupported message content part.")
        if part.get("type") in ("input_text", "output_text", "text"):
            if not isinstance(part.get("text"), str):
                raise HTTPException(400, "Text content must contain a string.")
            parts.append({"type": text_type, "text": part["text"]})
        elif part.get("type") == "input_image":
            parts.append(image_part(part))
        else:
            raise HTTPException(400, "Message content must contain text or images.")
    return parts


def tool_parameters(body: dict) -> dict:
    tools = body.get("tools", [])
    if tools is None:
        tools = []
    if not isinstance(tools, list):
        raise HTTPException(400, "tools must be an array of function definitions.")
    functions = []
    names = set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise HTTPException(400, "This connector supports Open WebUI function tools only.")
        callers = tool.get("allowed_callers")
        if (
            tool.get("defer_loading")
            or tool.get("async")
            or (
                callers is not None
                and (not isinstance(callers, list) or any(caller != "direct" for caller in callers))
            )
        ):
            raise HTTPException(400, "Only directly executed function tools are supported.")
        name = tool.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise HTTPException(400, "Tool names must be non-empty and unique.")
        names.add(name)
        functions.append(
            {
                key: tool[key]
                for key in ("type", "name", "description", "parameters", "strict")
                if key in tool
            }
        )
    payload = {}
    if functions:
        payload["tools"] = [
            {
                "type": "namespace",
                "name": TOOL_NAMESPACE,
                "description": "Tools selected and executed by Open WebUI.",
                "tools": functions,
            }
        ]
    choice = body.get("tool_choice")
    if choice not in (None, "auto", "none", "required"):
        raise HTTPException(400, "tool_choice must be auto, none, or required.")
    if choice == "required" and not functions:
        raise HTTPException(400, "tool_choice=required needs at least one function tool.")
    if choice is not None:
        payload["tool_choice"] = choice
    parallel = body.get("parallel_tool_calls")
    if parallel is not None:
        if not isinstance(parallel, bool):
            raise HTTPException(400, "parallel_tool_calls must be a boolean.")
        payload["parallel_tool_calls"] = parallel
    return payload


def tool_history(item: dict) -> dict:
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise HTTPException(400, "Tool history must contain a call_id.")
    if item.get("namespace") not in (None, TOOL_NAMESPACE):
        raise HTTPException(400, "Tool history contains an unsupported namespace.")
    result = {"type": item["type"], "call_id": call_id}
    if item["type"] == "function_call":
        if not isinstance(item.get("name"), str) or not item["name"]:
            raise HTTPException(400, "Function calls must contain a name.")
        if not isinstance(item.get("arguments"), str):
            raise HTTPException(400, "Function arguments must be a JSON string.")
        result.update(name=item["name"], arguments=item["arguments"], namespace=TOOL_NAMESPACE)
    else:
        output = item.get("output")
        result["output"] = (
            output if isinstance(output, str) else content_parts(output, "input_text")
        )
    return result


def responses_payload(body: dict) -> dict:
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise HTTPException(400, "A model is required.")

    configured_limit = next(
        (name for name in TOKEN_LIMIT_FIELDS if body.get(name) is not None), None
    )
    if configured_limit:
        raise HTTPException(
            400, f"SIWC does not support {configured_limit}. Clear the model's token limit."
        )
    stateful_field = next((name for name in STATEFUL_FIELDS if body.get(name) is not None), None)
    if stateful_field:
        raise HTTPException(
            400,
            f"This connector does not support {stateful_field}; send full history instead.",
        )
    if body.get("background"):
        raise HTTPException(400, "SIWC does not support background requests.")
    if body.get("response_format") is not None:
        raise HTTPException(400, "Use Responses text.format instead of response_format.")
    if body.get("truncation") not in (None, "disabled"):
        raise HTTPException(400, "Automatic history truncation is not supported.")
    tools = tool_parameters(body)

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        raw_input = [{"role": "user", "content": raw_input}]
    if not isinstance(raw_input, list) or not raw_input:
        raise HTTPException(400, "Input must contain the complete conversation history.")

    items = []
    calls = set()
    results = set()
    for item in raw_input:
        if not isinstance(item, dict):
            raise HTTPException(400, "Unsupported input item.")
        kind = item.get("type", "message")
        if kind == "reasoning":
            # With store=false, only encrypted reasoning can be replayed across requests.
            if item.get("encrypted_content"):
                items.append(
                    {
                        key: item[key]
                        for key in ("type", "id", "summary", "encrypted_content")
                        if key in item
                    }
                )
            continue
        if kind in ("function_call", "function_call_output"):
            history = tool_history(item)
            call_id = history["call_id"]
            if kind == "function_call":
                if call_id in calls:
                    raise HTTPException(400, "Tool history contains a duplicate call_id.")
                calls.add(call_id)
            else:
                if call_id not in calls or call_id in results:
                    raise HTTPException(
                        400, "Each tool result must follow its matching function call."
                    )
                results.add(call_id)
            items.append(history)
            continue
        if kind != "message":
            raise HTTPException(400, "History contains an unsupported Responses item.")

        role = "developer" if item.get("role") == "system" else item.get("role")
        if role not in ("user", "assistant", "developer"):
            raise HTTPException(400, "Unsupported message role.")
        text_type = "output_text" if role == "assistant" else "input_text"
        parts = content_parts(item.get("content", ""), text_type)
        items.append({"type": "message", "role": role, "content": parts})

    if calls != results:
        raise HTTPException(
            400, "Complete tool history must include a result for each function call."
        )
    if not items:
        raise HTTPException(400, "Input does not contain usable conversation context.")
    include = body.get("include") or []
    if not isinstance(include, list) or not all(isinstance(value, str) for value in include):
        raise HTTPException(400, "include must be an array of strings.")
    forwarded = ("instructions", "text", "service_tier", "prompt_cache_key")
    payload = {key: body[key] for key in forwarded if body.get(key) is not None}
    reasoning = reasoning_parameters(body)
    if reasoning is not None:
        payload["reasoning"] = reasoning
    payload.update(
        model=model,
        input=items,
        store=False,
        stream=True,
        include=list(dict.fromkeys([*include, "reasoning.encrypted_content"])),
    )
    payload.update(tools)
    return payload


async def sse_events(response: httpx.Response):
    lines = []
    size = 0
    async for line in response.aiter_lines():
        if line == "":
            if lines:
                data = "\n".join(lines)
                lines, size = [], 0
                if data != "[DONE]":
                    event = json.loads(data)
                    if not isinstance(event, dict):
                        raise ValueError("invalid SSE event")
                    yield event
        elif line.startswith("data:"):
            value = line[5:].lstrip(" ")
            lines.append(value)
            size += len(value)
            if size > 8 * 1024 * 1024:
                raise ValueError("SSE event too large")
    if lines:
        data = "\n".join(lines)
        if data != "[DONE]":
            event = json.loads(data)
            if not isinstance(event, dict):
                raise ValueError("invalid SSE event")
            yield event


def failure_event(
    message: str, code: str = "siwc_stream_interrupted", request_id: str | None = None
) -> dict:
    error = {"type": "server_error", "code": code, "message": message}
    if request_id:
        error["request_id"] = request_id
    return {"type": "response.failed", "response": {"status": "failed", "error": error}}


def event_frame(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


class ClosingStreamingResponse(StreamingResponse):
    def __init__(self, iterator, close, **kwargs):
        super().__init__(iterator, **kwargs)
        self.close_upstream = close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await asyncio.shield(self.close_upstream())


class ReasoningSummaries:
    """Preserve public summaries and their paragraph boundaries for Open WebUI."""

    def __init__(self):
        self._parts: dict[int, list[dict]] = {}
        self._indexes: dict[str, int] = {}

    def _part(self, output_index: int, summary_index: int, part: dict) -> dict:
        if not isinstance(summary_index, int) or summary_index < 0:
            raise ValueError("invalid reasoning summary index")
        parts = self._parts.setdefault(output_index, [])
        while len(parts) <= summary_index:
            parts.append({"type": "summary_text", "text": ""})
        previous = parts[summary_index]
        merged = {**previous, **part}
        if not part.get("text") and previous.get("text"):
            merged["text"] = previous["text"]
        parts[summary_index] = merged
        return merged

    def _display_part(self, output_index: int, summary_index: int) -> dict:
        parts = self._parts[output_index]
        part = dict(parts[summary_index])
        text = part.get("text", "")
        previous = next(
            (part.get("text") for part in reversed(parts[:summary_index]) if part.get("text")),
            "",
        )
        # Open WebUI concatenates summary parts without inserting a separator.
        if (
            text
            and previous
            and not text.startswith(("\n", "\r"))
            and not previous.endswith(("\n", "\r"))
        ):
            part["text"] = f"\n\n{text}"
        return part

    def _item(self, item: dict, output_index: int) -> dict:
        if item.get("type") != "reasoning":
            return item
        if item.get("id"):
            self._indexes[item["id"]] = output_index
        for index, part in enumerate(item.get("summary") or []):
            self._part(output_index, index, part)
        parts = self._parts.get(output_index)
        if not parts:
            return item
        summary = [self._display_part(output_index, index) for index in range(len(parts))]
        return item if summary == item.get("summary") else {**item, "summary": summary}

    def apply(self, event: dict) -> dict:
        kind = event["type"]
        if kind == "response.completed":
            response = event["response"]
            output = [
                self._item(item, self._indexes.get(item.get("id"), index))
                for index, item in enumerate(response["output"])
            ]
            return {**event, "response": {**response, "output": output}}

        output_index = event.get("output_index", self._indexes.get(event.get("item_id")))
        if not isinstance(output_index, int) or output_index < 0:
            return event
        if kind in {"response.output_item.added", "response.output_item.done"}:
            item = event.get("item")
            return (
                {**event, "item": self._item(item, output_index)}
                if isinstance(item, dict)
                else event
            )
        if not kind.startswith("response.reasoning_summary_"):
            return event

        summary_index = event.get("summary_index", 0)
        if kind in {
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_part.done",
        }:
            incoming = event.get("part")
            if isinstance(incoming, dict):
                self._part(output_index, summary_index, incoming)
                return {**event, "part": self._display_part(output_index, summary_index)}
        elif kind == "response.reasoning_summary_text.delta" and isinstance(
            event.get("delta"), str
        ):
            part = self._part(output_index, summary_index, {})
            first_text = not part["text"] and bool(event["delta"])
            part["text"] += event["delta"]
            if first_text:
                return {**event, "delta": self._display_part(output_index, summary_index)["text"]}
        elif kind == "response.reasoning_summary_text.done" and isinstance(event.get("text"), str):
            self._part(output_index, summary_index, {"text": event["text"]})
            return {**event, "text": self._display_part(output_index, summary_index)["text"]}
        return event


async def checked_events(result: httpx.Response):
    request_id = result.headers.get("x-request-id")
    summaries = ReasoningSummaries()
    try:
        async for event in sse_events(result):
            kind = event.get("type", "")
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") not in (
                None,
                "message",
                "reasoning",
                "function_call",
            ):
                yield failure_event(
                    "The upstream response requested an unsupported action.",
                    "siwc_unsupported_output",
                    request_id,
                )
                return
            if kind == "response.completed":
                response = event.get("response") or {}
                if response.get("status") != "completed" or not isinstance(
                    response.get("output"), list
                ):
                    raise ValueError("invalid completed response")
                if any(
                    item.get("type") not in ("message", "reasoning", "function_call")
                    for item in response["output"]
                ):
                    yield failure_event(
                        "The upstream response contained unsupported output.",
                        "siwc_unsupported_output",
                        request_id,
                    )
                    return
                yield summaries.apply(event)
                return
            if kind == "response.failed":
                error = (event.get("response") or {}).get("error")
                yield (
                    event
                    if isinstance(error, dict) and error
                    else failure_event("The OpenAI response failed.", request_id=request_id)
                )
                return
            if kind == "response.incomplete":
                yield failure_event(
                    "OpenAI returned response.incomplete; partial output is not a successful response.",
                    "siwc_response_incomplete",
                    request_id,
                )
                return
            if kind == "error":
                error = event.get("error")
                if not isinstance(error, dict):
                    error = {
                        "message": str(event.get("message", "OpenAI returned a streaming error.")),
                        "code": event.get("code", "siwc_stream_error"),
                    }
                yield {"type": "response.failed", "response": {"status": "failed", "error": error}}
                return
            if isinstance(kind, str) and kind.startswith("response."):
                yield summaries.apply(event)
        yield failure_event(
            "The stream ended before response.completed. The request was not retried.",
            request_id=request_id,
        )
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        yield failure_event(
            "The OpenAI stream failed or contained an invalid event.", request_id=request_id
        )


class ResponsesAPI:
    def __init__(self, auth: ChatGPTAuth):
        self.auth = auth
        self._snapshot: CatalogSnapshot | None = None
        self._catalog_lock = asyncio.Lock()

    async def _catalog(self, force: bool = False) -> CatalogSnapshot:
        snapshot = self._snapshot
        if not force and snapshot and time.monotonic() - snapshot.fetched_at < CATALOG_TTL:
            return snapshot
        async with self._catalog_lock:
            snapshot = self._snapshot
            if not force and snapshot and time.monotonic() - snapshot.fetched_at < CATALOG_TTL:
                return snapshot
            access = await self.auth.access_token()
            try:
                async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
                    result = await client.get(
                        f"{RESOURCE}/models", headers={"Authorization": f"Bearer {access}"}
                    )
            except httpx.HTTPError:
                raise HTTPException(503, "Could not reach the OpenAI model catalog.") from None
            if result.status_code != 200:
                raise CatalogUpstreamError(result)
            try:
                snapshot = catalog_snapshot(result.json())
            except (ValueError, KeyError, TypeError, AttributeError):
                raise HTTPException(
                    502, "The OpenAI model catalog did not match the SIWC schema."
                ) from None
            self._snapshot = snapshot
            return snapshot

    async def _model(self, model_id: str | None) -> CatalogModel | None:
        try:
            return (await self._catalog()).find(model_id)
        except (CatalogUpstreamError, HTTPException):
            return self._snapshot.find(model_id) if self._snapshot else None

    async def management_models(self) -> list[dict]:
        try:
            snapshot = await self._catalog(force=True)
        except CatalogUpstreamError as error:
            headers = {
                key: error.response.headers[key]
                for key in ("x-request-id", "retry-after")
                if error.response.headers.get(key)
            }
            raise HTTPException(
                error.response.status_code,
                "Could not load the ChatGPT model catalog.",
                headers=headers,
            ) from None
        return [
            {
                **model.api_model(),
                "reasoning_efforts": list(model.reasoning_efforts),
            }
            for model in snapshot.models
        ]

    async def models(self):
        try:
            snapshot = await self._catalog(force=True)
        except CatalogUpstreamError as error:
            return self._upstream_error(error.response)
        return JSONResponse(
            {"object": "list", "data": [model.api_model() for model in snapshot.models]},
            headers=NO_STORE,
        )

    @staticmethod
    def _upstream_error(result: httpx.Response):
        headers = dict(NO_STORE)
        for key in ("x-request-id", "retry-after"):
            if result.headers.get(key):
                headers[key] = result.headers[key]
        try:
            return JSONResponse(result.json(), status_code=result.status_code, headers=headers)
        except ValueError:
            return Response(
                result.content,
                status_code=result.status_code,
                media_type="text/plain",
                headers=headers,
            )

    async def responses(self, request: Request):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "The request body is not valid JSON.") from None
        if not isinstance(body, dict) or not isinstance(body.get("stream", False), bool):
            raise HTTPException(
                400, "The request must be a JSON object and stream must be a boolean."
            )
        payload = responses_payload(body)
        model_info = await self._model(payload["model"])
        reasoning = reasoning_payload(payload.get("reasoning"), model_info)
        if reasoning is not None:
            payload["reasoning"] = reasoning
        access = await self.auth.access_token()
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=20, write=30, pool=20), follow_redirects=False
        )
        result = None

        async def close():
            try:
                if result is not None:
                    await result.aclose()
            finally:
                await client.aclose()

        try:
            result = await client.send(
                client.build_request(
                    "POST",
                    f"{RESOURCE}/responses",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {access}",
                        "Content-Type": "application/json",
                    },
                ),
                stream=True,
            )
            if result.status_code != 200:
                await result.aread()
                error = self._upstream_error(result)
                await close()
                return error
        except BaseException as error:
            await asyncio.shield(close())
            if isinstance(error, httpx.HTTPError):
                raise HTTPException(
                    503, "The OpenAI request failed before a response was received."
                ) from None
            raise
        headers = {**NO_STORE, "X-Accel-Buffering": "no"}
        if result.headers.get("x-request-id"):
            headers["x-request-id"] = result.headers["x-request-id"]
        if body.get("stream", False):

            async def relay():
                async for event in checked_events(result):
                    yield event_frame(event)

            return ClosingStreamingResponse(
                relay(), close, media_type="text/event-stream", headers=headers
            )
        try:
            async for event in checked_events(result):
                if event["type"] == "response.completed":
                    return JSONResponse(event["response"], headers=headers)
                if event["type"] == "response.failed":
                    error = dict(event["response"]["error"])
                    if request_id := result.headers.get("x-request-id"):
                        error["request_id"] = request_id
                    return JSONResponse(
                        {"error": error},
                        status_code=ERROR_STATUS.get(error.get("code"), 502),
                        headers=headers,
                    )
            raise HTTPException(502, "The upstream stream ended without response.completed.")
        finally:
            await asyncio.shield(close())
