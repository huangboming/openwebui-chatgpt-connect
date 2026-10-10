"""Public stream/request behavior of the complete history Filter deliverable."""

import copy
import json
from pathlib import Path

import pytest

RECORDED_HISTORY = json.loads(
    (Path(__file__).parent / "fixtures" / "responses_history.json").read_text(encoding="utf-8")
)["cases"]

MODEL = {
    "id": "chatgpt.catalog-model",
    "openai": {"id": "catalog-model", "source": "chatgpt-connect"},
}
REASONING = {
    "type": "reasoning",
    "id": "reasoning-test",
    "summary": [],
    "encrypted_content": "opaque-test-data",
}
CALL = {
    "type": "function_call",
    "id": "call-item",
    "call_id": "call-test",
    "name": "get_marker",
    "namespace": "openwebui",
    "arguments": "{}",
}


def saved_details(history_filter, reasoning=REASONING):
    event = {
        "type": "response.completed",
        "response": {"status": "completed", "output": [reasoning, CALL]},
    }
    result = history_filter.stream(event, __model__=MODEL)
    return result["response"]["output"][0]["reasoning_details"]


@pytest.mark.parametrize("event_type", ["response.output_item.added", "response.output_item.done"])
@pytest.mark.parametrize("summary", [[], [{"type": "summary_text", "text": "Public summary."}]])
def test_reasoning_is_encoded_before_native_stream_storage(history_filter, event_type, summary):
    reasoning = {**REASONING, "summary": summary}
    event = {"type": event_type, "output_index": 0, "item": reasoning}
    original = copy.deepcopy(event)
    encoded = history_filter.stream(event, __model__=MODEL)
    assert event == original
    assert encoded["item"]["encrypted_content"] == reasoning["encrypted_content"]
    assert encoded["item"]["summary"] == summary
    detail = encoded["item"]["reasoning_details"][0]
    assert detail["data"] == reasoning["encrypted_content"]
    assert detail["summary"] == summary
    assert detail["model"] == "catalog-model"
    assert history_filter.stream(encoded, __model__=MODEL) == encoded


def test_completion_snapshot_keeps_every_reasoning_item(history_filter):
    event = {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": [REASONING, CALL, {**REASONING, "id": "second"}],
        },
    }
    original = copy.deepcopy(event)
    encoded = history_filter.stream(event, __model__=MODEL)
    assert event == original
    output = encoded["response"]["output"]
    assert output[1] == CALL
    assert [output[i]["reasoning_details"][0]["id"] for i in (0, 2)] == ["reasoning-test", "second"]
    assert history_filter.stream(encoded, __model__=MODEL) == encoded


@pytest.mark.parametrize("model", [{}, {"id": "other"}, {"openai": {"id": "catalog-model"}}])
def test_filter_is_scoped_by_connector_catalog_not_display_name(history_filter, model):
    event = {"type": "response.output_item.done", "item": REASONING}
    body = {"messages": [{"role": "assistant", "reasoning_details": [{"data": "other"}]}]}
    assert history_filter.stream(event, __model__=model) is event
    assert history_filter.request(body, __model__=model) is body


@pytest.mark.parametrize("encrypted", [None, ""])
def test_public_summary_without_encrypted_context_is_not_replayed(history_filter, encrypted):
    item = {**REASONING, "encrypted_content": encrypted}
    event = {"type": "response.output_item.done", "item": item}
    assert history_filter.stream(event, __model__=MODEL)["item"] == item


@pytest.mark.parametrize(
    "event",
    [
        {"choices": [{"delta": {"content": "Chat Completions"}}]},
        {"type": "response.failed", "response": {"error": {"message": "failed"}}},
        {"type": "response.function_call_arguments.delta", "delta": "{}"},
    ],
)
def test_other_stream_events_are_unchanged(history_filter, event):
    assert history_filter.stream(event, __model__=MODEL) is event


@pytest.mark.parametrize("has_text", [False, True])
def test_request_restores_context_without_replacing_tool_message(history_filter, has_text):
    details = json.loads(json.dumps(saved_details(history_filter)))
    tool_calls = [
        {
            "id": "call-test",
            "type": "function",
            "function": {"name": "get_marker", "arguments": "{}"},
        }
    ]
    message = {
        "role": "assistant",
        "content": "Checking." if has_text else "",
        "reasoning_details": details,
        "tool_calls": tool_calls,
    }
    body = {
        "messages": [
            {"role": "user", "content": "Get a marker."},
            message,
            {"role": "tool", "tool_call_id": "call-test", "content": "marker"},
        ]
    }
    original = copy.deepcopy(body)
    restored = history_filter.request(body, __model__=MODEL)
    assert body == original
    assert restored["messages"][1] == {"role": "assistant", "content": "", "output": [REASONING]}
    assert restored["messages"][2] == {k: v for k, v in message.items() if k != "reasoning_details"}
    assert restored["messages"][3] == body["messages"][2]
    assert history_filter.request(restored, __model__=MODEL) == restored


def test_new_filter_instance_restores_saved_context_and_visible_answer(history_filter):
    details = json.loads(json.dumps(saved_details(history_filter)))
    fresh = type(history_filter)()
    body = {"messages": [{"role": "assistant", "content": "Answer.", "reasoning_details": details}]}
    restored = fresh.request(body, __model__=MODEL)["messages"]
    assert restored == [
        {"role": "assistant", "content": "", "output": [REASONING]},
        {"role": "assistant", "content": "Answer."},
    ]


@pytest.mark.parametrize(
    "order",
    [
        ["reason-one", "call-one", "reason-two", "call-two"],
        ["reason-one", "reason-two", "call-one", "call-two"],
        ["call-one", "reason-one", "call-two", "reason-two"],
        ["reason-one", "message", "call-one"],
        ["reason-one", "message", "call-one", "reason-two", "call-two"],
    ],
)
def test_interleaved_reasoning_keeps_its_position_between_calls(history_filter, order):
    items = {
        "reason-one": {**REASONING, "id": "reason-one"},
        "reason-two": {**REASONING, "id": "reason-two"},
        "call-one": {**CALL, "call_id": "call-one"},
        "call-two": {**CALL, "call_id": "call-two"},
        "message": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Checking."}],
        },
    }
    event = {"type": "response.completed", "response": {"output": [items[key] for key in order]}}
    output = history_filter.stream(event, __model__=MODEL)["response"]["output"]
    details = [detail for item in output for detail in item.get("reasoning_details", [])]
    calls = [
        {"id": key, "type": "function", "function": {"name": "get_marker", "arguments": "{}"}}
        for key in order
        if key.startswith("call")
    ]
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": "Checking.",
                "tool_calls": calls,
                "reasoning_details": details,
            }
        ]
    }
    restored = history_filter.request(body, __model__=MODEL)
    restored_order = []
    for message in restored["messages"]:
        restored_order.extend(item["id"] for item in message.get("output", []))
        restored_order.extend(call["id"] for call in message.get("tool_calls", []))
    assert restored_order == [key for key in order if key != "message"]
    assert [message["content"] for message in restored["messages"] if message.get("content")] == [
        "Checking."
    ]
    assert history_filter.request(restored, __model__=MODEL) == restored


def test_model_switch_does_not_replay_another_models_encrypted_context(history_filter):
    details = saved_details(history_filter)
    other = {"id": "other-model", "openai": {"id": "other-model", "source": "chatgpt-connect"}}
    body = {"messages": [{"role": "assistant", "content": "Answer.", "reasoning_details": details}]}
    assert history_filter.request(body, __model__=other)["messages"] == [
        {"role": "assistant", "content": "Answer."}
    ]


def test_foreign_reasoning_details_are_preserved(history_filter):
    foreign = {"type": "reasoning.text", "text": "Foreign summary."}
    details = [foreign, *saved_details(history_filter)]
    body = {"messages": [{"role": "assistant", "content": "", "reasoning_details": details}]}
    result = history_filter.request(body, __model__=MODEL)["messages"]
    assert result[0]["output"] == [REASONING]
    assert result[1]["reasoning_details"] == [foreign]


@pytest.mark.parametrize(
    "change", [{"data": ""}, {"data": 12}, {"summary": None}, {"format": "unknown"}]
)
def test_invalid_context_fails_without_logging_opaque_data(history_filter, change, caplog):
    details = [{**saved_details(history_filter)[0], **change}]
    with pytest.raises(ValueError, match="saved ChatGPT reasoning context is invalid"):
        history_filter.request(
            {"messages": [{"role": "assistant", "reasoning_details": details}]}, __model__=MODEL
        )
    assert "opaque-test-data" not in caplog.text


@pytest.mark.parametrize("case", RECORDED_HISTORY, ids=lambda case: case["name"])
async def test_recorded_native_history_replays_through_both_deliveries(
    harness, history_filter, case
):
    await harness.connect()
    model = (await harness.models()).json()["data"][0]
    saved = json.loads(json.dumps(case["messages"]))
    fresh = type(history_filter)()
    restored = fresh.request({"messages": saved}, __model__={"id": model["id"], "openai": model})
    reasoning = [item for message in restored["messages"] for item in message.get("output", [])]
    assert reasoning == [item for item in case["input"] if item["type"] == "reasoning"]
    assert [message for message in restored["messages"] if message["role"] != "assistant"] == [
        message for message in saved if message["role"] != "assistant"
    ]
    order = []
    for message in restored["messages"]:
        order.extend((item["type"], item["id"]) for item in message.get("output", []))
        order.extend(("function_call", call["id"]) for call in message.get("tool_calls", []))
    assert order == [
        (item["type"], item.get("id") or item.get("call_id"))
        for item in case["input"]
        if item["type"] in ("reasoning", "function_call")
    ]

    # Use the recorded host projection instead of implementing another converter.
    replay = iter(reasoning)
    inputs = [next(replay) if item["type"] == "reasoning" else item for item in case["input"]]
    await harness.restart()
    response = await harness.respond(input=inputs)
    assert response.status_code == 200
    payload = json.loads(harness.upstream.calls("/v1/responses")[-1].content)
    assert payload["input"] == [
        {**item, "namespace": "openwebui"} if item["type"] == "function_call" else item
        for item in case["input"]
    ]
