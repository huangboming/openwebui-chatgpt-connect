"""Shared inline-document contract for the two standalone Functions."""

import base64
import mimetypes

MAX_FILE_BYTES = 50_000_000
FILE_LIMIT_ERROR = "File inputs exceed the 50 MB request limit."
DOCUMENT_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/rtf",
    "application/json",
    "application/javascript",
    "application/typescript",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/toml",
    "application/x-toml",
}
DOCUMENT_MIME_PREFIXES = (
    "text/",
    "application/vnd.openxmlformats-officedocument.",
    "application/vnd.oasis.opendocument.",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.ms-word",
)


def file_mime(filename: str, media_type: str | None = None) -> str:
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("File inputs must include a filename.")
    if media_type is not None and not isinstance(media_type, str):
        raise ValueError("File content type must be a string.")
    mime = (media_type or "").split(";", 1)[0].strip().lower()
    if mime in ("", "application/octet-stream"):
        mime = mimetypes.guess_type(filename)[0] or ""
    return mime


def document_mime(mime: str) -> str:
    if mime not in DOCUMENT_MIME_TYPES and not mime.startswith(DOCUMENT_MIME_PREFIXES):
        raise ValueError("Unsupported file format; use a document or spreadsheet.")
    return mime


def file_input_size(part: dict) -> int:
    data = part["file_data"]
    encoded_size = len(data) - data.index(",") - 1
    return encoded_size // 4 * 3 - data[-2:].count("=")


def file_input(part: dict) -> dict:
    if part.get("file_id") is not None or part.get("file_url") is not None:
        raise ValueError("SIWC file inputs require inline data, not file IDs or URLs.")
    data = part.get("file_data")
    if not isinstance(data, str) or not data.startswith("data:"):
        raise ValueError("file_data must be a base64 document data URL.")
    header, separator, encoded = data.partition(",")
    mime, encoding_separator, encoding = header.removeprefix("data:").partition(";")
    if not separator or not encoding_separator or encoding != "base64":
        raise ValueError("file_data must be a base64 document data URL.")
    filename = part.get("filename")
    document_mime(file_mime(filename, mime))
    if file_input_size(part) > MAX_FILE_BYTES:
        raise ValueError(FILE_LIMIT_ERROR)
    if len(encoded) % 4 or encoded.endswith("==="):
        raise ValueError("File data must contain valid, non-empty base64.")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise ValueError("File data must contain valid, non-empty base64.") from None
    if not decoded:
        raise ValueError("File data must contain valid, non-empty base64.")
    return {"type": "input_file", "filename": filename, "file_data": data}
