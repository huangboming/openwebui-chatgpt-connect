"""Adapt and relay OpenAI model catalog and Responses requests."""

import asyncio
import json
import time
from dataclasses import dataclass

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


def reasoning_payload(body: dict, model: CatalogModel | None) -> dict | None:
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
    supported = model.reasoning_efforts if model else ()
    if effort and supported and effort not in supported:
        raise HTTPException(
            400,
            f"Model {model.id} supports reasoning effort: {', '.join(supported)}.",
        )
    if effort and not supported and effort not in KNOWN_REASONING_EFFORTS:
        raise HTTPException(400, f"Unsupported reasoning effort: {effort}.")
    if effort:
        reasoning["effort"] = effort
    elif reasoning.get("effort") is None:
        reasoning.pop("effort", None)
    if raw is not None or effort or supported:
        if reasoning.get("summary") is None:
            reasoning["summary"] = "auto"
        return reasoning
    return None


def text_payload(body: dict, model_info: CatalogModel | None = None) -> dict:
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
            f"The text-only connector does not support {stateful_field}; send full history instead.",
        )
    if body.get("background"):
        raise HTTPException(400, "SIWC does not support background requests.")
    if body.get("response_format") is not None:
        raise HTTPException(400, "Use Responses text.format instead of response_format.")
    if body.get("truncation") not in (None, "disabled"):
        raise HTTPException(400, "Automatic history truncation is not supported.")
    if body.get("tool_choice") not in (None, "auto", "none"):
        raise HTTPException(400, "Forced tool calls are not supported.")

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        raw_input = [{"role": "user", "content": raw_input}]
    if not isinstance(raw_input, list) or not raw_input:
        raise HTTPException(400, "Input must contain the complete conversation history.")

    items = []
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
        if kind != "message":
            raise HTTPException(400, "Tool calls and other non-text history are not supported.")

        role = "developer" if item.get("role") == "system" else item.get("role")
        if role not in ("user", "assistant", "developer"):
            raise HTTPException(400, "Unsupported message role.")
        text_type = "output_text" if role == "assistant" else "input_text"
        content = item.get("content", "")
        if isinstance(content, str):
            parts = [{"type": text_type, "text": content}]
        elif isinstance(content, list):
            parts = []
            for part in content:
                if (
                    not isinstance(part, dict)
                    or part.get("type") not in ("input_text", "output_text", "text")
                    or not isinstance(part.get("text"), str)
                ):
                    raise HTTPException(
                        400, "Images, files, and other non-text content are not supported."
                    )
                parts.append({"type": text_type, "text": part["text"]})
        else:
            raise HTTPException(400, "Unsupported message content.")
        items.append({"type": "message", "role": role, "content": parts})

    if not items:
        raise HTTPException(400, "Input does not contain usable text context.")
    include = body.get("include") or []
    if not isinstance(include, list) or not all(isinstance(value, str) for value in include):
        raise HTTPException(400, "include must be an array of strings.")
    forwarded = ("instructions", "text", "service_tier", "prompt_cache_key")
    payload = {key: body[key] for key in forwarded if body.get(key) is not None}
    reasoning = reasoning_payload(body, model_info)
    if reasoning is not None:
        payload["reasoning"] = reasoning
    payload.update(
        model=model,
        input=items,
        store=False,
        stream=True,
        include=list(dict.fromkeys([*include, "reasoning.encrypted_content"])),
    )
    # Open WebUI adds tool declarations to text chats. This connector intentionally omits them.
    return payload


class StreamError(Exception):
    def __init__(self, error: dict, request_id: str | None = None):
        self.error = error
        self.request_id = request_id
        self.status = ERROR_STATUS.get(error.get("code"), 502)
        super().__init__(str(error.get("message", "The SIWC response did not complete.")))


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


async def checked_events(result: httpx.Response):
    request_id = result.headers.get("x-request-id")
    try:
        async for event in sse_events(result):
            kind = event.get("type", "")
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") not in (None, "message", "reasoning"):
                yield failure_event(
                    "The upstream response requested an unsupported non-text action.",
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
                    item.get("type") not in ("message", "reasoning") for item in response["output"]
                ):
                    yield failure_event(
                        "The upstream response contained unsupported non-text output.",
                        "siwc_unsupported_output",
                        request_id,
                    )
                    return
                yield event
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
                yield event
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
                if not force and snapshot:
                    return snapshot
                raise HTTPException(503, "Could not reach the OpenAI model catalog.") from None
            if result.status_code != 200:
                if not force and snapshot:
                    return snapshot
                raise CatalogUpstreamError(result)
            try:
                snapshot = catalog_snapshot(result.json())
            except (ValueError, KeyError, TypeError, AttributeError):
                if not force and self._snapshot:
                    return self._snapshot
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
        cached_model = self._snapshot.find(body.get("model")) if self._snapshot else None
        payload = text_payload(body, cached_model)
        model_info = await self._model(body.get("model"))
        if model_info != cached_model:
            payload = text_payload(body, model_info)
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
                    failure = StreamError(
                        event["response"]["error"], result.headers.get("x-request-id")
                    )
                    error = dict(failure.error)
                    if failure.request_id:
                        error["request_id"] = failure.request_id
                    return JSONResponse(
                        {"error": error}, status_code=failure.status, headers=headers
                    )
            raise HTTPException(502, "The upstream stream ended without response.completed.")
        finally:
            await asyncio.shield(close())
