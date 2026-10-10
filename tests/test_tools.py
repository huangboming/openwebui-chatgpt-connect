"""Tool request adaptation and streamed function calls at the delivery boundary."""

import asyncio
import json

import pytest

from tests.support import KEY, events

FUNCTION = {
    "type": "function",
    "name": "get_marker",
    "description": "Return a read-only test marker.",
    "parameters": {"type": "object", "properties": {}, "required": []},
    "strict": False,
}
REASONING = {
    "type": "reasoning",
    "id": "reasoning-tool",
    "summary": [],
    "encrypted_content": "opaque-tool-context",
}
CALL = {
    "type": "function_call",
    "id": "function-item",
    "call_id": "call-marker",
    "name": "get_marker",
    "namespace": "openwebui",
    "arguments": "{}",
    "status": "completed",
}
RESULT = {"type": "function_call_output", "call_id": "call-marker", "output": "read-only-marker"}


def tool_stream(calls=(CALL,)):
    output = [REASONING, *calls]
    stream = [{"type": "response.output_item.done", "output_index": 0, "item": REASONING}]
    for index, call in enumerate(calls, 1):
        stream.extend(
            [
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": {**call, "arguments": "", "status": "in_progress"},
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": call["id"],
                    "output_index": index,
                    "delta": call["arguments"],
                },
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": call["id"],
                    "output_index": index,
                    "arguments": call["arguments"],
                },
                {"type": "response.output_item.done", "output_index": index, "item": call},
            ]
        )
    stream.append(
        {
            "type": "response.completed",
            "response": {"id": "tool-response", "status": "completed", "output": output},
        }
    )
    return stream


@pytest.mark.parametrize("choice", [None, "auto", "none", "required"])
async def test_native_function_definitions_are_grouped_without_changing_names(harness, choice):
    await harness.connect()
    kwargs = {"tools": [FUNCTION], "parallel_tool_calls": False}
    if choice is not None:
        kwargs["tool_choice"] = choice
    response = await harness.respond(**kwargs)
    assert response.status_code == 200
    payload = json.loads(harness.upstream.calls("/v1/responses")[-1].content)
    assert payload["tools"] == [
        {
            "type": "namespace",
            "name": "openwebui",
            "description": "Tools selected and executed by Open WebUI.",
            "tools": [FUNCTION],
        }
    ]
    assert payload["parallel_tool_calls"] is False
    assert payload.get("tool_choice") == choice
    assert payload["store"] is False and payload["stream"] is True


@pytest.mark.parametrize("stream", [False, True])
async def test_function_call_response_is_relayed_without_executing_it(harness, stream):
    await harness.connect()
    harness.upstream.events = tool_stream()
    response = await harness.respond(stream=stream, tools=[FUNCTION])
    assert response.status_code == 200
    output = events(response)[-1]["response"]["output"] if stream else response.json()["output"]
    assert output == [REASONING, CALL]
    if stream:
        assert events(response) == harness.upstream.events
    assert len(harness.upstream.calls("/v1/responses")) == 1
    assert harness.upstream.streams[-1].closed


async def test_stateless_continuation_preserves_encrypted_context_tool_pair_and_image(harness):
    await harness.connect()
    image = {
        "type": "input_image",
        "image_url": "https://example.test/screenshot.png",
        "detail": "high",
    }
    history = [
        {"role": "user", "content": [{"type": "input_text", "text": "Use the tool."}, image]},
        REASONING,
        {k: v for k, v in CALL.items() if k != "namespace"},
        RESULT,
        {"role": "user", "content": "What was the result?"},
    ]
    await harness.restart()
    response = await harness.respond(input=history, tools=[FUNCTION])
    assert response.status_code == 200
    payload = json.loads(harness.upstream.calls("/v1/responses")[-1].content)
    assert payload["input"][0]["content"][1] == image
    assert payload["input"][1] == REASONING
    assert payload["input"][2] == {k: v for k, v in CALL.items() if k not in ("id", "status")}
    assert payload["input"][3] == RESULT
    assert payload["input"][4]["content"] == [
        {"type": "input_text", "text": "What was the result?"}
    ]
    assert "previous_response_id" not in payload


async def test_multiple_tool_pairs_and_error_results_are_replayed_in_order(harness):
    await harness.connect()
    second = {**CALL, "id": "function-two", "call_id": "call-two"}
    history = [
        {"role": "user", "content": "Get both markers."},
        REASONING,
        CALL,
        second,
        {**RESULT, "output": "Tool execution was rejected by the user."},
        {**RESULT, "call_id": "call-two", "output": "Tool failed: unavailable."},
    ]
    response = await harness.respond(input=history, tools=[FUNCTION])
    assert response.status_code == 200
    payload = json.loads(harness.upstream.calls("/v1/responses")[-1].content)
    assert [item["call_id"] for item in payload["input"][2:]] == [
        "call-marker",
        "call-two",
        "call-marker",
        "call-two",
    ]
    assert payload["input"][-2]["output"] == history[-2]["output"]
    assert payload["input"][-1]["output"] == history[-1]["output"]


async def test_tool_output_can_include_text_and_images(harness):
    await harness.connect()
    output = [
        {"type": "input_text", "text": "Screenshot result."},
        {"type": "input_image", "image_url": "https://example.test/result.png"},
    ]
    response = await harness.respond(
        input=[{"role": "user", "content": "Get screenshot."}, CALL, {**RESULT, "output": output}]
    )
    assert response.status_code == 200
    payload = json.loads(harness.upstream.calls("/v1/responses")[-1].content)
    assert payload["input"][-1]["output"] == [output[0], {**output[1], "detail": "auto"}]


@pytest.mark.parametrize(
    "changes",
    [
        {"tools": "invalid"},
        {"tools": False},
        {"tools": {}},
        {"tools": [{**FUNCTION, "defer_loading": True}]},
        {"tools": [{**FUNCTION, "async": True}]},
        {"tools": [{**FUNCTION, "allowed_callers": ["programmatic"]}]},
        {"tools": [{"type": "web_search"}]},
        {"tools": [{"type": "mcp"}]},
        {"tools": [{"type": "custom", "name": "custom"}]},
        {"tools": [{"type": "namespace", "name": "other", "tools": [FUNCTION]}]},
        {"tools": [FUNCTION, FUNCTION]},
        {"tools": [{"type": "function", "name": ""}]},
        {"tool_choice": "required"},
        {"tools": [FUNCTION], "tool_choice": {"type": "function", "name": "get_marker"}},
        {"parallel_tool_calls": "false"},
        {"input": [RESULT]},
        {"input": [CALL]},
        {"input": [CALL, {**RESULT, "call_id": "wrong"}]},
        {"input": [CALL, RESULT, RESULT]},
        {"input": [CALL, CALL, RESULT]},
        {"input": [{**CALL, "arguments": {}}, RESULT]},
        {"input": [{**CALL, "namespace": "other"}, RESULT]},
        {"input": [CALL, {**RESULT, "output": None}]},
    ],
)
async def test_invalid_or_unsupported_tools_fail_before_network_access(harness, changes):
    response = await harness.respond(**changes)
    assert response.status_code == 400
    assert not harness.upstream.requests


async def test_tool_stream_cancellation_after_partial_arguments_closes_upstream(harness):
    await harness.connect()
    harness.upstream.events = tool_stream()
    body = json.dumps(
        {"model": "catalog-model", "input": "Use the tool.", "stream": True, "tools": [FUNCTION]}
    ).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if b"response.function_call_arguments.delta" in message.get("body", b""):
            raise asyncio.CancelledError()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/siwc/v1/responses",
        "raw_path": b"/siwc/v1/responses",
        "root_path": "",
        "query_string": b"",
        "server": ("webui.example.test", 8080),
        "client": ("client", 1),
        "headers": [
            (b"host", b"webui.example.test:8080"),
            (b"authorization", f"Bearer {KEY}".encode()),
            (b"content-type", b"application/json"),
        ],
    }
    with pytest.raises(asyncio.CancelledError):
        await harness.app(scope, receive, send)
    assert harness.upstream.streams[-1].closed
    assert len(harness.upstream.calls("/v1/responses")) == 1


async def test_tool_stream_failure_is_not_retried(harness):
    await harness.connect()
    harness.upstream.events = tool_stream()[:-1]
    response = await harness.respond(stream=True, tools=[FUNCTION])
    assert response.status_code == 200
    assert events(response)[-1]["type"] == "response.failed"
    assert len(harness.upstream.calls("/v1/responses")) == 1
    assert harness.upstream.streams[-1].closed
