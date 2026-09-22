from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .errors import SchemaError


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp"}
DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
    ".rtf",
}
SENSITIVE_QUERY_KEYS = re.compile(
    r"(^|_)(auth|token|session|signature|credential|password|code)($|_)", re.I
)
MAX_BODY_JSON_BYTES = 12 * 1024 * 1024
MAX_TEXT_LENGTH = 20_000
MAX_DOCUMENT_CARDS = 2_000


def _require_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{name} must be an object")
    return value


def _require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise SchemaError(f"{name} must be an array")
    return value


def _id(value: Any, name: str) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise SchemaError(f"{name} must be an id")
    result = str(value)
    if not result or len(result) > 256:
        raise SchemaError(f"{name} is invalid")
    return result


def _optional_timestamp(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 128:
        raise SchemaError(f"{name} must be an ISO timestamp or null")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaError(f"{name} is not a valid ISO timestamp") from exc
    return value


def parse_courses(payload: Any) -> list[dict[str, str]]:
    groups = _require_list(payload, "courses response")
    courses: list[dict[str, str]] = []
    for group in groups:
        group_obj = _require_dict(group, "course group")
        candidates = group_obj.get("courses")
        if candidates is None:
            candidates = [group_obj]
        candidates = _require_list(candidates, "course group courses")
        for raw in candidates:
            course = _require_dict(raw, "course")
            course_id = _id(course.get("course_id", course.get("id")), "course_id")
            name = course.get("subject_name", course.get("name"))
            if not isinstance(name, str) or not name.strip() or len(name) > 500:
                raise SchemaError("course name is missing")
            courses.append({"course_id": course_id, "course": name.strip()})
    deduped = {item["course_id"]: item for item in courses}
    return list(deduped.values())


def _normalize_status(value: Any) -> str | None:
    if isinstance(value, bool):
        return "submitted" if value else "not_submitted"
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "submitted": "submitted",
        "not_submitted": "not_submitted",
        "unsubmitted": "not_submitted",
        "late": "late",
        "delayed": "late",
        "resubmission_needed": "resubmission_needed",
        "resubmit": "resubmission_needed",
        "unknown": "unknown",
    }
    return aliases.get(normalized)


def parse_assignment(
    raw: Any,
    course: dict[str, str],
    submitted_lookup: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    obj = _require_dict(raw, "submission")
    number = _id(
        obj.get("submission_number", obj.get("id")), "submission_number"
    )
    title = obj.get("message", obj.get("submission_message", obj.get("title")))
    # The API includes real submission boxes with an explicitly empty message.
    # Preserve that empty title; a missing or non-string field is still invalid.
    if not isinstance(title, str) or len(title) > 2000:
        raise SchemaError("submission title is missing")
    deadline = _optional_timestamp(obj.get("expiry", obj.get("deadline")), "deadline")
    updated_at = _optional_timestamp(
        obj.get("updated_at", obj.get("timestamp")), "assignment updated_at"
    )
    status = _normalize_status(obj.get("status"))
    if status is None:
        status = _normalize_status(obj.get("submitted"))
    if status is None and submitted_lookup is not None:
        lookup = submitted_lookup(number)
        if isinstance(lookup, dict):
            lookup = lookup.get("result", lookup.get("submitted"))
        status = _normalize_status(lookup)
    return {
        "id": number,
        "course_id": course["course_id"],
        "course": course["course"],
        "title": title.strip(),
        "deadline": deadline,
        "status": status or "unknown",
        "updated_at": updated_at,
    }


def parse_submissions_page(payload: Any) -> tuple[list[Any], str | None]:
    obj = _require_dict(payload, "submissions response")
    submissions = _require_list(obj.get("submissions"), "submissions")
    next_key = obj.get("next_page_key")
    if next_key is not None and not isinstance(next_key, (str, int)):
        raise SchemaError("next_page_key is invalid")
    return submissions, None if next_key is None else str(next_key)


def _next_page_key(payload: dict[str, Any]) -> str | None:
    next_key = payload.get("next_page_key")
    if next_key is not None and not isinstance(next_key, (str, int)):
        raise SchemaError("next_page_key is invalid")
    return None if next_key is None else str(next_key)


def parse_shared_notes_page(
    payload: Any,
) -> tuple[list[dict[str, Any]], list[str], str | None]:
    obj = _require_dict(payload, "shared notes response")
    entities = _require_list(obj.get("entities"), "shared note entities")
    notes: list[dict[str, Any]] = []
    group_ids: list[str] = []
    for raw in entities:
        entity = _require_dict(raw, "shared note entity")
        entity_type = entity.get("entity_type")
        if entity_type == "notes":
            note = _require_dict(entity.get("notes"), "shared note")
            if note.get("is_deleted") is not True:
                notes.append(note)
        elif entity_type == "note_groups":
            group = _require_dict(entity.get("note_groups"), "shared note group")
            group_ids.append(_id(group.get("id"), "shared note group id"))
        else:
            raise SchemaError("shared note entity type is unsupported")
    return notes, group_ids, _next_page_key(obj)


def parse_note_group_notes_page(
    payload: Any,
) -> tuple[list[dict[str, Any]], str | None]:
    obj = _require_dict(payload, "shared note group response")
    raw_notes = _require_list(obj.get("notes"), "shared note group notes")
    notes = []
    for raw in raw_notes:
        note = _require_dict(raw, "shared note")
        if note.get("is_deleted") is not True:
            notes.append(note)
    return notes, _next_page_key(obj)


def select_shared_note_protocol_version(
    payload: Any, supported_version: str
) -> str:
    obj = _require_dict(payload, "shared note protocol response")
    server_version = obj.get("latest_protocol_version")
    version_pattern = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
    if (
        not isinstance(server_version, str)
        or not version_pattern.fullmatch(server_version)
        or not version_pattern.fullmatch(supported_version)
    ):
        raise SchemaError("shared note protocol version is invalid")
    server_parts = tuple(int(part) for part in server_version.split("."))
    supported_parts = tuple(int(part) for part in supported_version.split("."))
    return server_version if server_parts < supported_parts else supported_version


def _safe_web_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(SENSITIVE_QUERY_KEYS.search(key) for key, _ in query):
        return None
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))


def _card_from_frame(frame: Any) -> dict[str, Any]:
    obj = _require_dict(frame, "document frame")
    raw_type = obj.get("type")
    if not isinstance(raw_type, str) or not raw_type:
        raise SchemaError("document frame type is missing")
    card: dict[str, Any] = {"type": "unknown", "raw_type": raw_type}
    if isinstance(obj.get("id"), (str, int)):
        card["id"] = str(obj["id"])

    gadgets = obj.get("gadgets")
    title = gadgets.get("title") if isinstance(gadgets, dict) else None
    if raw_type in {"title", "text"} and isinstance(title, dict):
        text = title.get("text")
        if isinstance(text, str):
            if len(text) > MAX_TEXT_LENGTH:
                raise SchemaError("text card exceeds the safety limit")
            card.update(type="text", text=text)
            return card
    content = obj.get("content")
    content = content if isinstance(content, dict) else {}
    if raw_type == "text" and isinstance(content.get("text"), str):
        text = content["text"]
        if len(text) > MAX_TEXT_LENGTH:
            raise SchemaError("text card exceeds the safety limit")
        card.update(type="text", text=text)
        return card
    if raw_type == "web":
        card["type"] = "web/link"
        safe_url = _safe_web_url(content.get("uri", content.get("url")))
        if safe_url:
            card["url"] = safe_url
        if isinstance(content.get("title"), str):
            card["title"] = content["title"][:1000]
        return card
    asset = content.get("asset")
    extension = None
    description = None
    if isinstance(asset, dict):
        extension = asset.get("extension")
        description = asset.get("description")
    extension = extension.lower() if isinstance(extension, str) else ""
    if extension and not extension.startswith("."):
        extension = "." + extension
    if raw_type == "picture" or extension in IMAGE_EXTENSIONS:
        card["type"] = "image"
    elif raw_type == "pdf" or extension in DOCUMENT_EXTENSIONS:
        card["type"] = "pdf/document"
    if extension:
        card["extension"] = extension
    if isinstance(description, str) and description:
        card["title"] = description[:1000]
    return card


def parse_document_cards(payload: bytes) -> list[dict[str, Any]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        raise SchemaError("LoiLoNote document is not a valid ZIP") from exc
    with archive:
        names = {PurePosixPath(name).as_posix() for name in archive.namelist()}
        if "body" not in names:
            raise SchemaError("LoiLoNote document body is missing")
        info = archive.getinfo("body")
        if info.file_size > MAX_BODY_JSON_BYTES:
            raise SchemaError("LoiLoNote document body exceeds the safety limit")
        try:
            raw = archive.read("body")
            body = json.loads(raw.decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaError("invalid LoiLoNote document body") from exc
    root = _require_dict(body, "document body")
    if "entire_body" in root:
        root = _require_dict(root["entire_body"], "document entire_body")
    data = root.get("data", root)
    data = _require_dict(data, "document data")
    frames = _require_list(data.get("frames"), "document frames")
    if len(frames) > MAX_DOCUMENT_CARDS:
        raise SchemaError("document card count exceeds the safety limit")
    return [_card_from_frame(frame) for frame in frames]


def parse_shared_note(
    raw: Any,
    course: dict[str, str],
    snapshot_loader: Callable[[str], bytes],
) -> dict[str, Any]:
    note = _require_dict(raw, "shared note")
    note_id = _id(note.get("id"), "shared note id")
    name = note.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 2000:
        raise SchemaError("shared note name is missing")
    created_at = _optional_timestamp(note.get("created_at"), "shared note created_at")
    updated_at = _optional_timestamp(note.get("updated_at"), "shared note updated_at")
    metadata_updated_at = _optional_timestamp(
        note.get("metadata_updated_at"), "shared note metadata_updated_at"
    )
    return {
        "id": note_id,
        "course_id": course["course_id"],
        "course": course["course"],
        "name": name.strip(),
        "created_at": created_at,
        "updated_at": updated_at,
        "metadata_updated_at": metadata_updated_at,
        "cards": parse_document_cards(snapshot_loader(note_id)),
    }


def parse_timeline_entry(
    raw: Any,
    course: dict[str, str],
    document_loader: Callable[[str], bytes],
) -> dict[str, Any] | None:
    obj = _require_dict(raw, "timeline entry")
    entry_type = obj.get("type")
    if entry_type != "share":
        return None
    entry_id = _id(obj.get("id"), "timeline entry id")
    sent_at = _optional_timestamp(obj.get("timestamp"), "timeline sent_at")
    if sent_at is None:
        raise SchemaError("timeline sent_at is missing")
    share = _require_dict(obj.get("share"), "timeline share")
    author = _require_dict(share.get("author"), "timeline author")
    sender = author.get("display_name", author.get("name"))
    if not isinstance(sender, str) or not sender.strip() or len(sender) > 1000:
        raise SchemaError("timeline sender is missing")
    document = _require_dict(share.get("document"), "timeline document")
    document_id = _id(document.get("id"), "timeline document id")
    cards = parse_document_cards(document_loader(document_id))
    return {
        "id": entry_id,
        "course_id": course["course_id"],
        "course": course["course"],
        "sender": sender.strip(),
        "sent_at": sent_at,
        "cards": cards,
    }


def parse_timeline_response(payload: Any) -> list[Any]:
    obj = _require_dict(payload, "timeline response")
    return _require_list(obj.get("entries"), "timeline entries")
