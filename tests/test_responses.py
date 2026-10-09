"""Final HTTP/SSE behavior and the outbound SIWC request contract."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from tests.support import KEY


def events(response):
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def reasoning_stream(texts, delivery, item_id="reasoning-1", output_index=0):
    item = {
        "id": item_id,
        "type": "reasoning",
        "summary": [],
        "encrypted_content": f"opaque-{item_id}",
    }
    stream = [{"type": "response.output_item.added", "output_index": output_index, "item": item}]
    for index, text in enumerate(texts):
        location = {"item_id": item_id, "output_index": output_index, "summary_index": index}
        stream.append(
            {
                "type": "response.reasoning_summary_part.added",
                **location,
                "part": {"type": "summary_text", "text": ""},
            }
        )
        if delivery == "delta":
            for delta in (text[: len(text) // 2], text[len(text) // 2 :]):
                stream.append(
                    {"type": "response.reasoning_summary_text.delta", **location, "delta": delta}
                )
        if delivery in {"delta", "text.done"}:
            stream.append(
                {
                    "type": "response.reasoning_summary_text.done",
                    **location,
                    "text": text if delivery == "text.done" else "",
                }
            )
        stream.append(
            {
                "type": "response.reasoning_summary_part.done",
                **location,
                "part": {"type": "summary_text", "text": text if delivery == "part.done" else ""},
            }
        )
    final_summary = [{"type": "summary_text", "text": text} for text in texts[:-1]]
    if final_summary:
        final_summary.append({"type": "summary_text", "text": ""})
    completed = {**item, "status": "completed", "summary": final_summary}
    stream.extend(
        [
            {"type": "response.output_item.done", "output_index": output_index, "item": completed},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        completed,
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Answer."}],
                        },
                    ],
                },
            },
        ]
    )
    return stream


async def test_responses_api_requires_subscription_authorization(harness):
    assert (await harness.models()).status_code == 503
    assert (await harness.respond()).status_code == 503
    assert not harness.upstream.requests


async def test_catalog_adapts_visible_models_without_hardcoding(harness):
    await harness.connect()
    response = await harness.models()
    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [
            {
                "id": "catalog-model",
                "name": "ChatGPT/Catalog Model",
                "object": "model",
                "owned_by": "openai",
            }
        ],
    }
    assert harness.upstream.calls("/v1/models")[0].headers["authorization"] == "Bearer access-1"


async def test_text_request_preserves_history_and_applies_siwc_constraints(harness):
    await harness.connect()
    await harness.models()
    response = await harness.respond(
        store=True,
        stream=False,
        temperature=0.7,
        metadata={"local": "private"},
        tools=[{"type": "function", "name": "unused"}],
        tool_choice="auto",
        instructions="rules",
        input=[
            {"type": "message", "role": "system", "content": "system rules", "id": "local"},
            {"role": "user", "content": [{"type": "input_text", "text": "first"}]},
            {
                "role": "assistant",
                "content": [{"type": "output_text", "text": "reply", "annotations": []}],
            },
            {"role": "user", "content": "second"},
        ],
    )
    assert response.status_code == 200
    payload = json.loads(harness.upstream.calls("/v1/responses")[0].content)
    assert payload["model"] == "catalog-model"
    assert payload["instructions"] == "rules"
    assert payload["store"] is False and payload["stream"] is True
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["reasoning"] == {"summary": "auto"}
    assert not {"tools", "tool_choice", "temperature", "metadata"} & payload.keys()
    assert payload["input"] == [
        {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "system rules"}],
        },
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first"}]},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "reply"}],
        },
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "second"}]},
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"max_output_tokens": 100},
        {"max_tokens": 100},
        {"max_completion_tokens": 100},
        {"previous_response_id": "old"},
        {"conversation": "old"},
        {"background": True},
        {"tool_choice": "required"},
        {"truncation": "auto"},
        {"response_format": {"type": "json_object"}},
        {"input": [{"type": "function_call_output", "call_id": "call", "output": "result"}]},
        {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "x"}]}]},
        {"input": []},
        {"stream": "true"},
        {"include": "invalid"},
        {"reasoning": "high"},
    ],
)
async def test_unsupported_requests_fail_before_network_or_semantic_loss(harness, changes):
    response = await harness.respond(**changes)
    assert response.status_code == 400
    assert not harness.upstream.requests


@pytest.mark.parametrize("summary", ["none", "concise", "detailed"])
async def test_explicit_reasoning_summary_is_preserved(harness, summary):
    await harness.connect()
    response = await harness.respond(reasoning={"effort": "medium", "summary": summary})
    assert response.status_code == 200
    payload = json.loads(harness.upstream.calls("/v1/responses")[0].content)
    assert payload["reasoning"] == {"effort": "medium", "summary": summary}


async def test_open_webui_reasoning_effort_is_adapted_and_model_checked(harness):
    await harness.connect()
    response = await harness.respond(reasoning_effort="low")
    assert response.status_code == 200
    payload = json.loads(harness.upstream.calls("/v1/responses")[0].content)
    assert payload["reasoning"] == {"effort": "low", "summary": "auto"}
    assert "reasoning_effort" not in payload

    unsupported = await harness.respond(reasoning_effort="high")
    assert unsupported.status_code == 400
    assert "supports reasoning effort: low, medium" in unsupported.json()["detail"]
    assert len(harness.upstream.calls("/v1/responses")) == 1


async def test_conflicting_reasoning_effort_fails_before_inference(harness):
    await harness.connect()
    response = await harness.respond(reasoning={"effort": "medium"}, reasoning_effort="low")
    assert response.status_code == 400
    assert "select different values" in response.json()["detail"]
    assert not harness.upstream.calls("/v1/responses")


async def test_first_inference_lazily_loads_reasoning_capabilities(harness):
    await harness.connect()
    await harness.models()
    await harness.restart()

    response = await harness.respond()
    assert response.status_code == 200
    assert len(harness.upstream.calls("/v1/models")) == 2
    payload = json.loads(harness.upstream.calls("/v1/responses")[0].content)
    assert payload["reasoning"] == {"summary": "auto"}

    await harness.respond()
    assert len(harness.upstream.calls("/v1/models")) == 2


@pytest.mark.parametrize("effort,status", [("high", 200), ("low", 400)])
async def test_reasoning_effort_uses_the_refreshed_catalog(harness, monkeypatch, effort, status):
    now = time.monotonic()
    monkeypatch.setattr(
        harness.module, "time", SimpleNamespace(time=time.time, monotonic=lambda: now)
    )
    await harness.connect()
    await harness.models()
    harness.upstream.models[0]["supported_reasoning_levels"] = ["medium", "high"]
    now += 301

    response = await harness.respond(reasoning_effort=effort)
    assert response.status_code == status
    assert len(harness.upstream.calls("/v1/models")) == 2
    if status == 200:
        payload = json.loads(harness.upstream.calls("/v1/responses")[0].content)
        assert payload["reasoning"] == {"effort": effort, "summary": "auto"}
    else:
        assert "medium, high" in response.json()["detail"]
        assert not harness.upstream.calls("/v1/responses")


async def test_first_inference_accepts_an_effort_advertised_by_the_model(harness):
    await harness.connect()
    harness.upstream.models[0]["supported_reasoning_levels"] = ["future-effort"]
    response = await harness.respond(reasoning_effort="future-effort")
    assert response.status_code == 200
    payload = json.loads(harness.upstream.calls("/v1/responses")[0].content)
    assert payload["reasoning"] == {"effort": "future-effort", "summary": "auto"}


@pytest.mark.parametrize("cached", [False, True])
async def test_inference_tolerates_catalog_failure_but_explicit_refresh_does_not(
    harness, monkeypatch, cached
):
    now = time.monotonic()
    monkeypatch.setattr(
        harness.module, "time", SimpleNamespace(time=time.time, monotonic=lambda: now)
    )
    await harness.connect()
    if cached:
        await harness.models()
        now += 301
    harness.upstream.model_status = 503

    response = await harness.respond(reasoning_effort="low")
    assert response.status_code == 200
    payload = json.loads(harness.upstream.calls("/v1/responses")[0].content)
    assert payload["reasoning"] == {"effort": "low", "summary": "auto"}
    assert (await harness.models()).status_code == 503
    assert (await harness.client.get("/siwc/admin/models")).status_code == 503


async def test_reasoning_replay_keeps_only_replayable_content(harness):
    await harness.connect()
    response = await harness.respond(
        input=[
            {"role": "user", "content": "hello"},
            {"type": "reasoning", "id": "unresolvable", "summary": []},
            {
                "type": "reasoning",
                "id": "replay",
                "summary": [],
                "encrypted_content": "encrypted",
                "started_at": 1,
            },
        ]
    )
    assert response.status_code == 200
    payload = json.loads(harness.upstream.calls("/v1/responses")[0].content)
    assert len(payload["input"]) == 2
    assert payload["input"][1] == {
        "type": "reasoning",
        "id": "replay",
        "summary": [],
        "encrypted_content": "encrypted",
    }


async def test_reasoning_summary_events_are_relayed_for_open_webui(harness):
    await harness.connect()
    await harness.models()
    harness.upstream.events = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "reasoning-1", "type": "reasoning", "summary": []},
        },
        {
            "type": "response.reasoning_summary_part.added",
            "output_index": 0,
            "summary_index": 0,
            "part": {"type": "summary_text", "text": ""},
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "output_index": 0,
            "summary_index": 0,
            "delta": "Checking the request.",
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "reasoning-1",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Checking the request."}],
            },
        },
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "id": "reasoning-1",
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "Checking the request."}],
                    },
                    {"type": "message", "role": "assistant", "content": []},
                ],
            },
        },
    ]
    response = await harness.respond(stream=True)
    assert response.status_code == 200
    assert events(response) == harness.upstream.events
    payload = json.loads(harness.upstream.calls("/v1/responses")[0].content)
    assert payload["reasoning"] == {"summary": "auto"}


@pytest.mark.parametrize(
    "texts,expected_texts",
    [
        (["Checking the request."], ["Checking the request."]),
        (["First summary.", "Last summary."], ["First summary.", "\n\nLast summary."]),
    ],
)
@pytest.mark.parametrize("delivery", ["delta", "text.done", "part.done"])
@pytest.mark.parametrize("stream", [False, True])
async def test_completion_preserves_public_reasoning_summaries(
    harness, texts, expected_texts, delivery, stream
):
    await harness.connect()
    harness.upstream.events = reasoning_stream(texts, delivery)
    response = await harness.respond(stream=stream)
    assert response.status_code == 200
    expected = [{"type": "summary_text", "text": text} for text in expected_texts]
    if stream:
        received = events(response)
        assert [event["type"] for event in received] == [
            event["type"] for event in harness.upstream.events
        ]
        done = next(event for event in received if event["type"] == "response.output_item.done")
        assert done["item"]["summary"] == expected
        completed = received[-1]["response"]
    else:
        completed = response.json()
    assert completed["output"][0]["summary"] == expected
    assert completed["output"][0]["encrypted_content"] == "opaque-reasoning-1"
    assert completed["output"][1] == harness.upstream.events[-1]["response"]["output"][1]
    assert harness.upstream.streams[-1].closed


@pytest.mark.parametrize("part_added", [False, True])
async def test_streamed_summary_paragraphs_keep_their_boundaries(harness, part_added):
    await harness.connect()
    texts = [
        "Analyzing candy draw strategy with tactile discrimination",
        "Calculating minimum guaranteed candy draws with shape selection",
        "Evaluating shape-based draw allocation for guarantee",
    ]
    upstream = reasoning_stream(texts, "delta")
    harness.upstream.events = [
        event
        for event in upstream
        if part_added or event["type"] != "response.reasoning_summary_part.added"
    ]
    response = await harness.respond(stream=True)
    received = events(response)
    deltas = [
        event for event in received if event["type"] == "response.reasoning_summary_text.delta"
    ]
    expected = "\n\n".join(texts)
    assert "".join(event["delta"] for event in deltas) == expected
    for index, text in enumerate(texts):
        chunks = [event["delta"] for event in deltas if event["summary_index"] == index]
        assert chunks[-1] == text[len(text) // 2 :]
    for event in received:
        if event["type"] == "response.output_item.done":
            assert "".join(part["text"] for part in event["item"]["summary"]) == expected
    assert (
        "".join(part["text"] for part in received[-1]["response"]["output"][0]["summary"])
        == expected
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "texts,expected",
    [
        (["First.", "Second.", "Third."], "First.\n\nSecond.\n\nThird."),
        (["First.\n\n", "Second."], "First.\n\nSecond."),
        (["First.", "\n\nSecond."], "First.\n\nSecond."),
        (["First.\n", "Second."], "First.\nSecond."),
        (["First.", "\nSecond."], "First.\nSecond."),
        (["One paragraph\nwith a line break."], "One paragraph\nwith a line break."),
        (["", "Only visible paragraph."], "Only visible paragraph."),
    ],
)
async def test_completed_summary_paragraph_boundaries(harness, stream, texts, expected):
    await harness.connect()
    item = {
        "id": "reasoning-1",
        "type": "reasoning",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": text} for text in texts],
        "encrypted_content": "opaque-reasoning-1",
    }
    harness.upstream.events = [
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {"status": "completed", "output": [item]}},
    ]
    response = await harness.respond(stream=stream)
    completed = events(response)[-1]["response"] if stream else response.json()
    assert "".join(part["text"] for part in completed["output"][0]["summary"]) == expected
    assert completed["output"][0]["encrypted_content"] == "opaque-reasoning-1"


async def test_reasoning_summaries_are_kept_separate_by_item(harness):
    await harness.connect()
    first = reasoning_stream(["First item."], "delta")
    second = reasoning_stream(["Second item."], "text.done", "reasoning-2", 1)
    for event in second:
        if event["type"].startswith("response.reasoning_summary_"):
            event.pop("output_index")
    output = [first[-1]["response"]["output"][0], *second[-1]["response"]["output"]]
    harness.upstream.events = [
        *first[:-1],
        *second[:-1],
        {"type": "response.completed", "response": {"status": "completed", "output": output}},
    ]
    response = await harness.respond(stream=True)
    completed = events(response)[-1]["response"]["output"]
    assert [item["summary"][0]["text"] for item in completed[:2]] == ["First item.", "Second item."]


async def test_nonempty_final_reasoning_summary_is_authoritative(harness):
    await harness.connect()
    harness.upstream.events = reasoning_stream(["Draft summary."], "delta")
    authoritative = [{"type": "summary_text", "text": "Final summary."}]
    harness.upstream.events[-1]["response"]["output"][0] = {
        **harness.upstream.events[-2]["item"],
        "summary": authoritative,
    }
    response = await harness.respond(stream=True)
    received = events(response)
    assert received[-2]["item"]["summary"] == [{"type": "summary_text", "text": "Draft summary."}]
    assert received[-1]["response"]["output"][0]["summary"] == authoritative


async def test_reasoning_done_events_without_text_preserve_streamed_summaries(harness):
    await harness.connect()
    harness.upstream.events = reasoning_stream(["Streamed summary."], "delta")
    for event in harness.upstream.events:
        if event["type"] == "response.reasoning_summary_text.done":
            event.pop("text")
        elif event["type"] == "response.reasoning_summary_part.done":
            event.pop("part")
    response = await harness.respond(stream=True)
    assert events(response)[-1]["response"]["output"][0]["summary"] == [
        {"type": "summary_text", "text": "Streamed summary."}
    ]


async def test_encrypted_reasoning_without_a_public_summary_is_unchanged(harness):
    await harness.connect()
    harness.upstream.events = reasoning_stream(["Previous request summary."], "delta")
    assert (await harness.respond(stream=True)).status_code == 200
    harness.upstream.events = reasoning_stream([], "delta")
    response = await harness.respond(stream=True)
    assert events(response) == harness.upstream.events


async def test_stream_handles_fragmented_utf8_and_closes_upstream(harness):
    await harness.connect()
    response = await harness.respond(stream=True)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-request-id"] == "request-test"
    assert events(response) == harness.upstream.events
    assert events(response)[0]["delta"] == "Price: \u20ac5"
    assert harness.upstream.streams[-1].closed
    assert len(harness.upstream.calls("/v1/responses")) == 1


async def test_non_stream_task_returns_completed_response(harness):
    await harness.connect()
    response = await harness.respond(stream=False)
    assert response.status_code == 200
    assert response.json() == harness.upstream.events[-1]["response"]
    assert json.loads(harness.upstream.calls("/v1/responses")[0].content)["stream"] is True
    assert harness.upstream.streams[-1].closed


@pytest.mark.parametrize(
    "upstream_events,code",
    [
        ([{"type": "response.output_text.delta", "delta": "partial"}], "siwc_stream_interrupted"),
        ([{"type": "response.incomplete"}], "siwc_response_incomplete"),
        (
            [
                {
                    "type": "response.output_item.added",
                    "item": {"type": "function_call", "name": "danger"},
                }
            ],
            "siwc_unsupported_output",
        ),
        (
            [
                {
                    "type": "response.completed",
                    "response": {"status": "completed", "output": [{"type": "function_call"}]},
                }
            ],
            "siwc_unsupported_output",
        ),
        (
            [{"type": "response.completed", "response": {"status": "incomplete", "output": []}}],
            "siwc_stream_interrupted",
        ),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_unfinished_or_unsupported_output_is_never_success(
    harness, upstream_events, code, stream
):
    await harness.connect()
    harness.upstream.events = upstream_events
    response = await harness.respond(stream=stream)
    if stream:
        result = events(response)
        assert result[-1]["type"] == "response.failed"
        assert result[-1]["response"]["error"]["code"] == code
        assert not any(item["type"] == "response.completed" for item in result)
    else:
        assert response.status_code == 502
        assert response.json()["error"]["code"] == code
        assert response.json()["error"]["request_id"] == "request-test"
    assert harness.upstream.streams[-1].closed
    assert len(harness.upstream.calls("/v1/responses")) == 1


async def test_non_stream_subscription_limit_error_is_mapped(harness):
    await harness.connect()
    harness.upstream.events = [
        {
            "type": "response.failed",
            "response": {
                "status": "failed",
                "error": {
                    "code": "subscription_sharing_usage_limit_exceeded",
                    "message": "limit",
                },
            },
        }
    ]
    response = await harness.respond()
    assert response.status_code == 429
    assert response.json()["error"]["request_id"] == "request-test"
    assert harness.upstream.streams[-1].closed


async def test_upstream_http_error_preserves_status_and_retry_headers(harness):
    await harness.connect()
    harness.upstream.response_status = 429
    response = await harness.respond()
    assert response.status_code == 429
    assert response.json() == harness.upstream.response_error
    assert response.headers["retry-after"] == "30"
    assert response.headers["x-request-id"] == "request-test"
    assert len(harness.upstream.calls("/v1/responses")) == 1


async def test_cancellation_before_first_output_closes_upstream(harness):
    await harness.connect()
    body = json.dumps({"model": "catalog-model", "input": "hello", "stream": True}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def cancelled_send(message):
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
        await harness.app(scope, receive, cancelled_send)
    assert harness.upstream.streams[-1].closed
    assert len(harness.upstream.calls("/v1/responses")) == 1
