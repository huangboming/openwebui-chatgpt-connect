"""Image inputs through the complete, built Responses endpoint."""

import base64
import json
from pathlib import Path

import pytest

PNG = (
    "data:image/png;base64,"
    + base64.b64encode(
        (
            Path(__file__).resolve().parents[1] / "src/openwebui_chatgpt_connect/assets/chatgpt.png"
        ).read_bytes()
    ).decode()
)
IMAGE_URL = "https://images.example.test/screenshot.png?signature=example"


@pytest.mark.parametrize("image_url", [PNG, IMAGE_URL])
async def test_image_inputs_are_forwarded_without_local_downloads(harness, image_url):
    await harness.connect()
    response = await harness.respond(
        input=[{"role": "user", "content": [{"type": "input_image", "image_url": image_url}]}]
    )
    assert response.status_code == 200
    payload = json.loads(harness.upstream.calls("/v1/responses")[0].content)
    assert payload["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_image", "image_url": image_url, "detail": "auto"}],
        }
    ]
    assert payload["store"] is False and payload["stream"] is True
    assert {request.url.host for request in harness.upstream.requests} == {
        "auth.openai.com",
        "api.openai.com",
    }


@pytest.mark.parametrize("detail", ["auto", "low", "high", "original"])
async def test_image_detail_is_preserved(harness, detail):
    await harness.connect()
    part = {"type": "input_image", "image_url": PNG, "detail": detail}
    response = await harness.respond(input=[{"role": "user", "content": [part]}])
    assert response.status_code == 200
    payload = json.loads(harness.upstream.calls("/v1/responses")[0].content)
    assert payload["input"][0]["content"] == [part]


@pytest.mark.parametrize("media_type", ["image/png", "image/jpeg", "image/webp", "image/gif"])
async def test_supported_image_data_urls_preserve_the_encoded_bytes(harness, media_type):
    await harness.connect()
    encoded = base64.b64encode(b"image bytes decoded by the upstream model").decode()
    image_url = f"data:{media_type};base64,{encoded}"
    response = await harness.respond(
        input=[{"role": "user", "content": [{"type": "input_image", "image_url": image_url}]}]
    )
    assert response.status_code == 200
    payload = json.loads(harness.upstream.calls("/v1/responses")[0].content)
    assert payload["input"][0]["content"][0]["image_url"] == image_url


@pytest.mark.parametrize("stream", [False, True])
async def test_image_chats_preserve_mixed_content_and_complete_history(harness, stream):
    await harness.connect()
    first_image = {"type": "input_image", "image_url": PNG, "detail": "auto"}
    second_image = {"type": "input_image", "image_url": IMAGE_URL, "detail": "high"}
    history = [
        {"type": "message", "role": "developer", "content": "Read visible labels."},
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Compare these screenshots."},
                first_image,
                {"type": "input_text", "text": "This is the second screenshot."},
                second_image,
            ],
        },
        {"type": "reasoning", "id": "reasoning", "summary": [], "encrypted_content": "opaque"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "The labels differ."}],
        },
        {"type": "message", "role": "user", "content": "What does the first one say?"},
    ]
    response = await harness.respond(input=history, stream=stream)
    assert response.status_code == 200
    if stream:
        assert "response.completed" in response.text
    else:
        assert response.json()["status"] == "completed"
    payload = json.loads(harness.upstream.calls("/v1/responses")[0].content)
    expected = [
        {**history[0], "content": [{"type": "input_text", "text": history[0]["content"]}]},
        *history[1:-1],
        {**history[-1], "content": [{"type": "input_text", "text": history[-1]["content"]}]},
    ]
    assert payload["input"] == expected
    assert payload["reasoning"] == {"summary": "auto"}


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_image"},
        {"type": "input_image", "file_id": "file-test"},
        {"type": "input_image", "file_id": "file-test", "image_url": IMAGE_URL},
        *[
            {"type": "input_image", "image_url": value}
            for value in (
                None,
                42,
                {"url": IMAGE_URL},
                "",
                " ",
                "image.png",
                "/api/v1/files/image/content",
                "http://images.example.test/image.png",
                "file:///tmp/image.png",
                "blob:https://webui.example.test/image",
                "https://",
                "https:///image.png",
                "https://[invalid/image.png",
                "https://user:password@images.example.test/image.png",
                "https://images.example.test/image\n.png",
                "data:image/svg+xml;base64,PHN2Zy8+",
                "data:application/pdf;base64,JVBERg==",
                "data:image/png,raw-bytes",
                "data:image/png;base64",
                "data:image/png;base64,",
                "data:image/png;base64,not!base64",
                "data:image/png;base64,YQ",
                "data:image/png;base64,\u00e9",
            )
        ],
        *[
            {"type": "input_image", "image_url": IMAGE_URL, "detail": value}
            for value in (None, "", "invalid", 42, [])
        ],
        {"type": "input_file", "file_data": "document"},
    ],
)
async def test_invalid_image_and_file_inputs_fail_before_network(harness, part):
    response = await harness.respond(
        input=[
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Read this."}, part],
            }
        ]
    )
    assert response.status_code == 400
    assert response.json()["detail"]
    assert not harness.upstream.requests


@pytest.mark.parametrize(
    "error",
    [
        {"code": "invalid_image", "message": "The image could not be decoded."},
        {"code": "unsupported_image", "message": "This model does not support image inputs."},
    ],
)
async def test_upstream_image_rejections_are_returned_without_retry(harness, error):
    await harness.connect()
    harness.upstream.response_status = 400
    harness.upstream.response_error = {"error": error}
    response = await harness.respond(
        input=[{"role": "user", "content": [{"type": "input_image", "image_url": PNG}]}],
        stream=True,
    )
    assert response.status_code == 400
    assert response.json() == {"error": error}
    assert response.headers["x-request-id"] == "request-test"
    assert len(harness.upstream.calls("/v1/responses")) == 1
