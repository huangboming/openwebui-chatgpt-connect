"""Original document inputs through the two complete Function deliveries."""

import base64
import copy
import json
import mimetypes
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support import install_modules

MODEL = {"id": "catalog-model", "openai": {"id": "catalog-model", "source": "chatgpt-connect"}}


@pytest.fixture
def uploaded_files(host, tmp_path, monkeypatch):
    records = {}
    grants = set()
    reads = []
    paths = {}

    async def get_file_by_id(file_id):
        return records.get(file_id)

    async def has_access_to_file(file_id, access, user):
        return access == "read" and (
            records[file_id].user_id == user.id or (file_id, user.id) in grants
        )

    def file_meta(data):
        content_type = data.get("content_type")
        if isinstance(content_type, list):
            data["content_type"] = next(
                (value for value in content_type if isinstance(value, str)), None
            )
        elif content_type is not None and not isinstance(content_type, str):
            data["content_type"] = None
        return SimpleNamespace(
            name=data.get("name"), content_type=data.get("content_type"), size=data.get("size")
        )

    def get_file(path):
        reads.append(path)
        return paths.get(path, path)

    def upload(name="document.pdf", data=b"%PDF-1.7\noriginal bytes", owner="user"):
        file_id = f"upload-{len(records)}"
        path = tmp_path / file_id
        path.write_bytes(data)
        record = SimpleNamespace(
            id=file_id,
            user_id=owner,
            filename=f"stored-{name}",
            path=str(path),
            meta={"name": name, "content_type": mimetypes.guess_type(name)[0]},
        )
        records[file_id] = record
        return {
            "type": "file",
            "id": file_id,
            "name": name,
            "file": {
                "id": file_id,
                "path": "/untrusted/request/path",
                "filename": "untrusted-name.txt",
                "data": {"content": "EXTRACTED TEXT MUST NOT BE SENT"},
            },
        }

    bindings = {
        "open_webui.models.files": {
            "Files": SimpleNamespace(get_file_by_id=get_file_by_id),
            "FileMeta": SimpleNamespace(model_validate=file_meta),
        },
        "open_webui.storage": {},
        "open_webui.storage.provider": {"Storage": SimpleNamespace(get_file=get_file)},
        "open_webui.utils.access_control": {},
        "open_webui.utils.access_control.files": {"has_access_to_file": has_access_to_file},
    }
    install_modules(monkeypatch, bindings)
    return SimpleNamespace(upload=upload, records=records, grants=grants, reads=reads, paths=paths)


def file_part(name, data, media_type=None):
    mime = media_type or mimetypes.guess_type(name)[0]
    return {
        "type": "input_file",
        "filename": name,
        "file_data": f"data:{mime};base64,{base64.b64encode(data).decode()}",
    }


@pytest.mark.parametrize(
    "name", ["document.pdf", "notes.txt", "report.docx", "slides.pptx", "data.csv", "data.xlsx"]
)
async def test_original_documents_are_forwarded_without_byte_or_filename_changes(harness, name):
    await harness.connect()
    part = file_part(name, b"original file bytes\x00\xff")
    response = await harness.respond(input=[{"role": "user", "content": [part]}])
    assert response.status_code == 200
    payload = json.loads(harness.upstream.calls("/v1/responses")[-1].content)
    assert payload["input"] == [{"type": "message", "role": "user", "content": [part]}]
    assert payload["store"] is False and payload["stream"] is True
    assert not harness.upstream.calls("/v1/files")


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_file", "filename": "doc.pdf", "file_id": "local-id"},
        {"type": "input_file", "filename": "doc.pdf", "file_url": "https://example.test/doc.pdf"},
        {"type": "input_file", "file_data": "data:application/pdf;base64,YQ=="},
        {"type": "input_file", "filename": "doc.pdf", "file_data": "raw bytes"},
        {"type": "input_file", "filename": "doc.pdf", "file_data": "data:application/pdf;base64,"},
        {"type": "input_file", "filename": "doc.pdf", "file_data": "data:application/pdf;base64,!"},
        {
            "type": "input_file",
            "filename": "doc.pdf",
            "file_data": "data:application/pdf;base64,YWJj====",
        },
        file_part("audio.wav", b"audio"),
        file_part("movie.mp4", b"video"),
        file_part("archive.zip", b"archive"),
    ],
)
async def test_invalid_and_unsupported_file_inputs_fail_before_network(harness, part):
    response = await harness.respond(input=[{"role": "user", "content": [part]}])
    assert response.status_code == 400
    assert not harness.upstream.requests


@pytest.mark.parametrize("location", ["messages", "tool-output"])
async def test_file_size_limit_covers_complete_request_input(harness, monkeypatch, location):
    monkeypatch.setattr(harness.module, "MAX_FILE_BYTES", 8)
    part = file_part("notes.txt", b"12345")
    inputs = [{"role": "user", "content": [part]}]
    if location == "messages":
        inputs.append({"role": "user", "content": [part]})
    else:
        inputs.extend(
            [
                {
                    "type": "function_call",
                    "call_id": "call-file",
                    "name": "read_file",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "call-file", "output": [part]},
            ]
        )
    response = await harness.respond(input=inputs)
    assert response.status_code == 400
    assert "limit" in response.json()["detail"]
    assert not harness.upstream.requests


@pytest.mark.parametrize("stream", [False, True])
async def test_upstream_document_rejection_is_explicit_and_not_retried(harness, stream):
    await harness.connect()
    error = {"code": "unsupported_file", "message": "This model does not accept the file format."}
    harness.upstream.response_status = 400
    harness.upstream.response_error = {"error": error}
    response = await harness.respond(
        input=[{"role": "user", "content": [file_part("report.docx", b"document")]}], stream=stream
    )
    assert response.status_code == 400
    assert response.json() == {"error": error}
    assert len(harness.upstream.calls("/v1/responses")) == 1


@pytest.mark.parametrize(
    "content_type",
    [
        ["application/pdf", None],
        [None, "application/pdf"],
        " APPLICATION/PDF; charset=binary ",
        "application/octet-stream",
    ],
)
async def test_upload_mime_uses_host_metadata_without_mutating_the_record(
    history_filter, uploaded_files, content_type
):
    upload = uploaded_files.upload()
    record = uploaded_files.records[upload["id"]]
    record.meta["content_type"] = content_type
    original = copy.deepcopy(record.meta)
    result = await history_filter.inlet(
        {"files": [upload], "messages": [{"role": "user", "content": "Read it."}]},
        __model__=MODEL,
        __user__={"id": "user"},
    )
    data = result["messages"][0]["content"][0]["file"]["file_data"]
    assert data.startswith("data:application/pdf;base64,")
    assert record.meta == original


@pytest.mark.parametrize("length", range(1, 8))
async def test_inline_file_size_boundary_with_all_base64_padding_lengths(
    harness, monkeypatch, length
):
    await harness.connect()
    monkeypatch.setattr(harness.module, "MAX_FILE_BYTES", 5)
    part = file_part("notes.txt", b"a" * length)
    response = await harness.respond(input=[{"role": "user", "content": [part]}])
    assert response.status_code == (200 if length <= 5 else 400)
    if length <= 5:
        payload = json.loads(harness.upstream.calls("/v1/responses")[-1].content)
        assert payload["input"][0]["content"] == [part]
    else:
        assert not harness.upstream.calls("/v1/responses")


async def test_inlet_uses_authorized_originals_as_current_attachment_context(
    history_filter, uploaded_files
):
    first = uploaded_files.upload("first.pdf", b"original PDF")
    second = uploaded_files.upload("second.csv", b"name,value\na,2\n")
    knowledge = {"type": "file", "id": "knowledge-file", "collection_name": "knowledge"}
    image = {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}}
    body = {
        "files": [first, knowledge, first, second],
        "messages": [
            {"role": "user", "content": "Earlier question."},
            {"role": "assistant", "content": "Earlier answer."},
            {"role": "user", "content": [{"type": "text", "text": "Compare these."}, image]},
        ],
    }
    original = copy.deepcopy(body)
    result = await history_filter.inlet(body, __model__=MODEL, __user__={"id": "user"})
    assert body == original
    assert result["files"] == [knowledge]
    assert result["messages"][:2] == body["messages"][:2]
    parts = result["messages"][-1]["content"]
    assert [part["file"]["filename"] for part in parts[:2]] == ["first.pdf", "second.csv"]
    assert [base64.b64decode(part["file"]["file_data"].split(",", 1)[1]) for part in parts[:2]] == [
        b"original PDF",
        b"name,value\na,2\n",
    ]
    assert parts[2:] == body["messages"][-1]["content"]
    assert len(uploaded_files.reads) == 2
    assert "/untrusted/request/path" not in uploaded_files.reads
    assert "EXTRACTED TEXT" not in json.dumps(result)
    assert await history_filter.inlet(result, __model__=MODEL, __user__={"id": "user"}) == result
    assert len(uploaded_files.reads) == 2


@pytest.mark.parametrize("role", ["owner", "admin", "granted", "denied"])
async def test_file_access_uses_native_ownership_admin_and_access_grants(
    history_filter, uploaded_files, role
):
    file = uploaded_files.upload(owner="user" if role == "owner" else "other")
    user = "admin" if role == "admin" else "user"
    if role == "granted":
        uploaded_files.grants.add((file["id"], user))
    body = {"files": [file], "messages": [{"role": "user", "content": "Read this."}]}
    if role == "denied":
        with pytest.raises(ValueError, match="not accessible"):
            await history_filter.inlet(body, __model__=MODEL, __user__={"id": user})
        assert not uploaded_files.reads
    else:
        result = await history_filter.inlet(body, __model__=MODEL, __user__={"id": user})
        assert result["files"] == []
        assert len(uploaded_files.reads) == 1


@pytest.mark.parametrize(
    "failure",
    [
        "no-user",
        "missing-record",
        "missing-bytes",
        "empty",
        "unsupported",
        "limit",
        "no-user-message",
        "no-id",
    ],
)
async def test_unusable_attachments_fail_without_sending_extracted_text(
    history_filter, uploaded_files, monkeypatch, failure, caplog
):
    file = uploaded_files.upload()
    record = uploaded_files.records[file["id"]]
    user = {"id": "user"}
    body = {"files": [file], "messages": [{"role": "user", "content": "Read it."}]}
    if failure == "no-user":
        user = None
    elif failure == "missing-record":
        uploaded_files.records.clear()
    elif failure == "missing-bytes":
        record.path = "/missing/file.pdf"
    elif failure == "empty":
        Path(record.path).write_bytes(b"")
    elif failure == "unsupported":
        record.meta.update(name="audio.wav", content_type="audio/wav")
    elif failure == "limit":
        monkeypatch.setitem(history_filter.inlet.__globals__, "MAX_FILE_BYTES", 4)
    elif failure == "no-user-message":
        body["messages"] = [{"role": "assistant", "content": "Answer."}]
    elif failure == "no-id":
        file.pop("id")
        file["file"].pop("id")
    original = copy.deepcopy(body)
    with pytest.raises(ValueError):
        await history_filter.inlet(body, __model__=MODEL, __user__=user)
    assert body == original
    assert "original bytes" not in caplog.text


async def test_storage_provider_resolves_remote_objects_and_attachment_only_messages(
    history_filter, uploaded_files
):
    file = uploaded_files.upload("report.docx", b"remote original bytes")
    record = uploaded_files.records[file["id"]]
    uploaded_files.paths["s3://bucket/file"] = record.path
    record.path = "s3://bucket/file"
    body = {"files": [file], "messages": [{"role": "user", "content": ""}]}
    result = await history_filter.inlet(body, __model__=MODEL, __user__={"id": "user"})
    assert uploaded_files.reads == ["s3://bucket/file"]
    assert len(result["messages"][0]["content"]) == 1
    part = result["messages"][0]["content"][0]["file"]
    assert part == {
        key: value
        for key, value in file_part("report.docx", b"remote original bytes").items()
        if key != "type"
    }


async def test_other_providers_knowledge_and_image_attachments_are_not_changed(
    history_filter, uploaded_files
):
    file = uploaded_files.upload()
    body = {"files": [file], "messages": [{"role": "user", "content": "Read it."}]}
    assert await history_filter.inlet(body, __model__={"id": "other"}) is body
    for files in (
        [{"type": "collection", "id": "knowledge"}],
        [{"type": "file", "id": "knowledge-file"}],
        [{"type": "image", "url": "https://example.test/image.png"}],
        [{**file, "legacy": True}],
    ):
        unchanged = {**body, "files": files}
        assert await history_filter.inlet(unchanged, __model__=MODEL) is unchanged
    assert not uploaded_files.reads


async def test_fresh_filter_rebuilds_file_context_after_history_reload(
    history_filter, uploaded_files
):
    file = uploaded_files.upload("notes.txt", b"persistent original")
    first = {"files": [file], "messages": [{"role": "user", "content": "Read these notes."}]}
    await history_filter.inlet(first, __model__=MODEL, __user__={"id": "user"})
    saved = json.loads(json.dumps(first))
    saved["messages"].extend(
        [
            {"role": "assistant", "content": "Answer."},
            {"role": "user", "content": "What about the final paragraph?"},
        ]
    )
    fresh = type(history_filter)()
    result = await fresh.inlet(saved, __model__=MODEL, __user__={"id": "user"})
    assert result["messages"][:-1] == saved["messages"][:-1]
    assert (
        result["messages"][-1]["content"][0]["file"]["file_data"]
        == file_part("notes.txt", b"persistent original")["file_data"]
    )
    assert len(uploaded_files.reads) == 2


async def test_known_oversize_files_fail_before_storage_download(history_filter, uploaded_files):
    file = uploaded_files.upload()
    uploaded_files.records[file["id"]].meta["size"] = 50_000_001
    with pytest.raises(ValueError, match="50 MB"):
        await history_filter.inlet(
            {"files": [file], "messages": [{"role": "user", "content": "Read it."}]},
            __model__=MODEL,
            __user__={"id": "user"},
        )
    assert not uploaded_files.reads


async def test_storage_failures_do_not_expose_provider_details(
    history_filter, uploaded_files, monkeypatch, caplog
):
    file = uploaded_files.upload()

    def unavailable(path):
        raise ValueError("private-storage-detail")

    monkeypatch.setattr(sys.modules["open_webui.storage.provider"].Storage, "get_file", unavailable)
    with pytest.raises(ValueError, match="unavailable in Open WebUI storage") as error:
        await history_filter.inlet(
            {"files": [file], "messages": [{"role": "user", "content": "Read it."}]},
            __model__=MODEL,
            __user__={"id": "user"},
        )
    assert "private-storage-detail" not in str(error.value)
    assert "private-storage-detail" not in caplog.text


@pytest.mark.parametrize(
    "content_type", [None, "image/png", "application/octet-stream", ["image/png", None]]
)
async def test_image_upload_records_keep_the_native_image_path(
    history_filter, uploaded_files, content_type
):
    image = uploaded_files.upload("image.png", b"image")
    uploaded_files.records[image["id"]].meta["content_type"] = content_type
    body = {"files": [image], "messages": [{"role": "user", "content": "Look at the image."}]}
    assert await history_filter.inlet(body, __model__=MODEL, __user__={"id": "user"}) is body
    assert not uploaded_files.reads
