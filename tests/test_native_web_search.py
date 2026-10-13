"""Native web access uses the chat switch, host policy, and original response stream."""

import copy
import json

import pytest

from tests.support import events

MODEL = {"openai": {"id": "catalog-model", "source": "chatgpt-connect"}}
REASONING = {
    "type": "reasoning",
    "id": "rs_test",
    "summary": [],
    "encrypted_content": "opaque-test",
}
SEARCH = {
    "type": "web_search_call",
    "id": "ws_test",
    "status": "completed",
    "action": {"type": "search", "query": "today's news"},
}
CALL = {"type": "function_call", "call_id": "call-test", "name": "get_marker", "arguments": "{}"}
CITATION = {"type": "url_citation", "url": "https://example.test/news", "title": "News"}
ANSWER = {
    "type": "message",
    "role": "assistant",
    "content": [{"type": "output_text", "text": "Today's news.", "annotations": [CITATION]}],
}


@pytest.fixture
def web_enabled(host):
    host.config.update(
        {"web.search.enable": True, "user.permissions": {"features": {"web_search": True}}}
    )
    return host


async def test_chat_switch_adds_hosted_search_without_changing_other_tools_or_parameters(
    history_filter, web_enabled
):
    function = {"type": "function", "function": {"name": "get_marker"}}
    body = {
        "features": {"web_search": True, "code_interpreter": True},
        "tools": [function],
        "tool_choice": "auto",
        "reasoning_effort": "medium",
        "messages": [{"role": "user", "content": "Search today's news."}],
    }
    original = copy.deepcopy(body)
    result = await history_filter.inlet(body, __model__=MODEL, __user__={"id": "user"})
    assert body == original
    assert result == {
        **body,
        "features": {**body["features"], "web_search": False},
        "tools": [function, {"type": "web_search"}],
    }
    assert await history_filter.inlet(result, __model__=MODEL, __user__={"id": "user"}) == result
    assert await history_filter.inlet(body, __model__={}, __user__={"id": "user"}) is body
    off = {**body, "features": {"web_search": False}}
    assert await history_filter.inlet(off, __model__=MODEL, __user__={"id": "user"}) is off


@pytest.mark.parametrize("disabled", ["global", "permission", "capability", "builtin"])
async def test_chat_switch_cannot_bypass_host_web_search_policy(
    history_filter, web_enabled, disabled
):
    model = copy.deepcopy(MODEL)
    if disabled == "global":
        web_enabled.config["web.search.enable"] = False
    elif disabled == "permission":
        web_enabled.config["user.permissions"]["features"]["web_search"] = False
    else:
        key = "capabilities" if disabled == "capability" else "builtinTools"
        model["info"] = {"meta": {key: {"web_search": False}}}
    body = (
        {"tools": [{"type": "web_search"}]}
        if disabled == "permission"
        else {"features": {"web_search": True}}
    )
    with pytest.raises(ValueError, match="[Ww]eb [Ss]earch"):
        await history_filter.inlet(body, __model__=model, __user__={"id": "user"})


@pytest.mark.parametrize("enabled", [False, True])
async def test_hosted_actions_and_citations_are_relayed_only_when_requested(harness, enabled):
    await harness.connect()
    completed = {"status": "completed", "output": [REASONING, SEARCH, ANSWER]}
    harness.upstream.events = [
        {"type": "response.output_item.added", "item": {**SEARCH, "status": "in_progress"}},
        {"type": "response.web_search_call.completed", "output_index": 1},
        {"type": "response.output_item.done", "item": SEARCH},
        {"type": "response.output_text.annotation.added", "annotation": CITATION},
        {"type": "response.completed", "response": completed},
    ]
    tools = [{"type": "function", "name": "get_marker", "parameters": {"type": "object"}}]
    if enabled:
        tools.append({"type": "web_search"})
    response = await harness.respond(tools=tools, reasoning_effort="medium", stream=True)
    received = events(response)
    payload = json.loads(harness.upstream.calls("/v1/responses")[-1].content)
    assert payload["tools"][0]["type"] == "namespace"
    assert payload["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert ("web_search_call.action.sources" in payload["include"]) is enabled
    if enabled:
        assert payload["tools"][1] == {"type": "web_search"}
        assert received == harness.upstream.events
    else:
        assert received[-1]["type"] == "response.failed"
        assert received[-1]["response"]["error"]["code"] == "siwc_unsupported_output"
    assert harness.upstream.streams[-1].closed


async def test_progress_and_retrieved_links_use_native_ui_without_rewriting_output(
    history_filter,
):
    page = "https://example.test/report"
    search = {
        **SEARCH,
        "action": {
            **SEARCH["action"],
            "sources": [
                {"url": CITATION["url"], "title": CITATION["title"]},
                {"url": page, "title": "Report"},
                {"url": page, "title": "Duplicate"},
                {"url": "https://example.test/overview"},
                {"url": "javascript:alert(1)"},
                {"url": "https://secret@example.test/private"},
            ],
        },
    }
    frames = [
        {"type": "response.web_search_call.in_progress"},
        {"type": "response.web_search_call.searching"},
        {"type": "response.output_item.done", "item": search},
        {
            "type": "response.output_item.done",
            "item": {
                **SEARCH,
                "action": {"type": "open_page", "url": page},
            },
        },
        {"type": "response.completed", "response": {"output": [search, ANSWER]}},
    ]
    original = copy.deepcopy(frames)
    emitted = []

    async def emit(event):
        emitted.append(event)

    for frame in frames:
        assert await history_filter.stream(frame, __model__=MODEL, __event_emitter__=emit) == frame
    assert frames == original
    statuses = [event["data"] for event in emitted if event["type"] == "status"]
    assert [status["description"] for status in statuses] == [
        "Accessing the web",
        "Searching the web",
        "Search completed",
        "Page-opening step completed",
        "Web-enabled response completed",
    ]
    assert [status["done"] for status in statuses] == [False, False, True, True, True]
    sources = [event["data"] for event in emitted if event["type"] == "source"]
    displayed = {source["metadata"][0]["source"]: source for source in sources}
    assert [source["source"]["url"] for source in displayed.values()] == [
        CITATION["url"],
        page,
        "https://example.test/overview",
    ]
    assert sources[1]["source"]["name"] == "Search result: Report"
    # Native source IDs deduplicate item/final notifications; streamed links
    # still exist when the final response has no output snapshot.
    emitted.clear()
    await history_filter.stream(frames[2], __model__=MODEL, __event_emitter__=emit)
    await history_filter.stream(
        {"type": "response.completed", "response": {"output": []}},
        __model__=MODEL,
        __event_emitter__=emit,
        __body__={"tools": [{"type": "web_search"}]},
    )
    assert [event["data"] for event in emitted if event["type"] == "source"] == list(
        displayed.values()
    )
    assert emitted[-1]["data"]["done"] is True
    assert all(source["metadata"][0]["type"] == "web_search_result" for source in sources)
    assert all("not necessarily opened or cited" in source["document"][0] for source in sources)
    assert all(not source["metadata"][0]["source"].startswith("http") for source in sources)


@pytest.mark.parametrize("kind", ["response.failed", "response.incomplete"])
async def test_web_response_failure_finishes_progress_without_publishing_results(
    history_filter,
    kind,
):
    emitted = []

    async def emit(event):
        emitted.append(event)

    event = {"type": kind, "response": {"output": [SEARCH, ANSWER]}}
    assert await history_filter.stream(event, __model__=MODEL, __event_emitter__=emit) is event
    assert emitted == [
        {
            "type": "status",
            "data": {
                "action": "web_search",
                "description": "Web-enabled response failed",
                "done": True,
                "error": True,
            },
        }
    ]


async def test_optional_ui_delivery_does_not_break_history_or_affect_other_models(history_filter):
    emitted = []

    async def unavailable(event):
        emitted.append(event)
        raise RuntimeError("UI unavailable")

    snapshot = {"type": "response.completed", "response": {"output": [REASONING, SEARCH, ANSWER]}}
    encoded = await history_filter.stream(
        snapshot,
        __model__=MODEL,
        __event_emitter__=unavailable,
    )
    assert encoded == await history_filter.stream(snapshot, __model__=MODEL)
    assert (
        encoded["response"]["output"][0]["reasoning_details"][0]["data"]
        == REASONING["encrypted_content"]
    )
    assert len(emitted) == 1
    assert (
        await history_filter.stream(
            snapshot,
            __model__={},
            __event_emitter__=unavailable,
        )
        is snapshot
    )
    assert len(emitted) == 1
    ordinary = {"type": "response.completed", "response": {"output": [ANSWER]}}
    assert (
        await history_filter.stream(
            ordinary,
            __model__=MODEL,
            __event_emitter__=unavailable,
        )
        == ordinary
    )
    assert len(emitted) == 1


async def test_saved_web_actions_replay_in_order_with_reasoning_and_function_calls(
    history_filter, harness
):
    leading = {**SEARCH, "id": "ws_leading"}
    trailing = {
        **SEARCH,
        "id": "ws_trailing",
        "action": {"type": "open_page", "url": CITATION["url"]},
    }
    later_reasoning = {**REASONING, "id": "rs_later", "encrypted_content": "opaque-later"}
    output = [leading, REASONING, SEARCH, CALL, trailing, later_reasoning, ANSWER]
    snapshot = {"type": "response.completed", "response": {"status": "completed", "output": output}}
    encoded = await history_filter.stream(snapshot, __model__=MODEL)
    assert snapshot["response"]["output"] == output
    assert await history_filter.stream(encoded, __model__=MODEL) == encoded
    details = [
        detail
        for item in encoded["response"]["output"]
        for detail in item.get("reasoning_details", [])
    ]
    body = json.loads(
        json.dumps(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "reasoning_details": details,
                        "content": "",
                        "tool_calls": [
                            {
                                "id": CALL["call_id"],
                                "type": "function",
                                "function": {"name": CALL["name"], "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": CALL["call_id"], "content": "marker"},
                ]
            }
        )
    )
    restored = history_filter.request(body, __model__=MODEL)
    replay = []
    for message in restored["messages"]:
        replay.extend(message.get("output", []))
        if message.get("tool_calls"):
            replay.append(CALL)
    assert replay == output[:-1]
    assert history_filter.request(restored, __model__=MODEL) == restored
    await harness.connect()
    result = {"type": "function_call_output", "call_id": CALL["call_id"], "output": "marker"}
    response = await harness.respond(
        input=[*replay, result, ANSWER], tools=[{"type": "web_search"}]
    )
    assert response.status_code == 200
    sent = json.loads(harness.upstream.calls("/v1/responses")[-1].content)["input"]
    assert [item["type"] for item in sent] == [item["type"] for item in [*replay, result, ANSWER]]
    assert [item for item in sent if item["type"] == "web_search_call"] == [
        leading,
        SEARCH,
        trailing,
    ]
