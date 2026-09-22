from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import SchemaError


SCHEMA_VERSION = 1
MAX_CACHED_TEXT_LENGTH = 20_000


def default_cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / ".local" / "share")
    return Path(base) / "LoiLoMorningBriefing"


def _timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{name} must be a timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaError(f"{name} is invalid") from exc
    return value


def _validate_cards(cards: Any, name: str) -> None:
    if not isinstance(cards, list):
        raise SchemaError(f"{name} cards must be an array")
    for card in cards:
        if not isinstance(card, dict) or card.get("type") not in {
            "text",
            "image",
            "pdf/document",
            "web/link",
            "unknown",
        }:
            raise SchemaError(f"invalid {name} card")
        if card.get("type") == "text" and (
            not isinstance(card.get("text"), str)
            or len(card["text"]) > MAX_CACHED_TEXT_LENGTH
        ):
            raise SchemaError(f"invalid {name} text card")


def validate_cache(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SchemaError("cache must be an object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise SchemaError("unsupported cache schema version")
    _timestamp(data.get("fetched_at"), "fetched_at")
    if data.get("status") not in {"success", "partial"}:
        raise SchemaError("invalid cache status")
    for key in ("courses", "assignments", "timeline", "course_status", "errors"):
        if not isinstance(data.get(key), list):
            raise SchemaError(f"cache {key} must be an array")
    for course in data["courses"]:
        if not isinstance(course, dict) or not isinstance(course.get("course_id"), str):
            raise SchemaError("invalid cached course")
    for assignment in data["assignments"]:
        if not isinstance(assignment, dict) or assignment.get("status") not in {
            "submitted",
            "not_submitted",
            "late",
            "resubmission_needed",
            "unknown",
        }:
            raise SchemaError("invalid cached assignment")
    for entry in data["timeline"]:
        if not isinstance(entry, dict):
            raise SchemaError("invalid cached timeline entry")
        _validate_cards(entry.get("cards"), "cached timeline")
    if "shared_notes" in data:
        if not isinstance(data["shared_notes"], list):
            raise SchemaError("cache shared_notes must be an array")
        for note in data["shared_notes"]:
            if (
                not isinstance(note, dict)
                or not isinstance(note.get("id"), str)
                or not isinstance(note.get("course_id"), str)
                or not isinstance(note.get("name"), str)
                or not isinstance(note.get("cards"), list)
            ):
                raise SchemaError("invalid cached shared note")
            if not note["name"].strip() or len(note["name"]) > 2000:
                raise SchemaError("invalid cached shared note name")
            for key in ("created_at", "updated_at", "metadata_updated_at"):
                value = note.get(key)
                if value is not None:
                    _timestamp(value, f"shared note {key}")
            if "stale" in note and not isinstance(note["stale"], bool):
                raise SchemaError("invalid cached shared note stale flag")
            _validate_cards(note["cards"], "cached shared note")
    return data


def _atomic_json_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


class CacheStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or default_cache_dir()
        self.cache_path = self.directory / "loilonote-cache.json"
        self.state_path = self.directory / "loilonote-state.json"

    def load_cache(self) -> dict[str, Any] | None:
        if not self.cache_path.exists():
            return None
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaError("LoiLoNote cache is corrupt") from exc
        return validate_cache(data)

    def write_cache(self, data: dict[str, Any]) -> None:
        validate_cache(data)
        _atomic_json_write(self.cache_path, data)

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": SCHEMA_VERSION, "courses": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaError("LoiLoNote state is corrupt") from exc
        if (
            not isinstance(data, dict)
            or data.get("schema_version") != SCHEMA_VERSION
            or not isinstance(data.get("courses"), dict)
        ):
            raise SchemaError("invalid LoiLoNote state")
        return data

    def write_state(self, data: dict[str, Any]) -> None:
        if data.get("schema_version") != SCHEMA_VERSION or not isinstance(
            data.get("courses"), dict
        ):
            raise SchemaError("invalid LoiLoNote state")
        _atomic_json_write(self.state_path, data)

    def briefing_input(self, stale_after_hours: float = 30.0) -> dict[str, Any]:
        try:
            cache = self.load_cache()
        except SchemaError as exc:
            return {
                "source": "loilonote",
                "status": "unavailable",
                "reason": "cache_corrupt",
                "message": str(exc),
            }
        if cache is None:
            return {
                "source": "loilonote",
                "status": "unavailable",
                "reason": "cache_missing",
                "message": "LoiLoNote authentication required or collector has not succeeded yet",
            }
        fetched = datetime.fromisoformat(cache["fetched_at"].replace("Z", "+00:00"))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        age_hours = max(
            0.0, (datetime.now(timezone.utc) - fetched.astimezone(timezone.utc)).total_seconds() / 3600
        )
        return {
            "source": "loilonote",
            "status": "stale" if age_hours > stale_after_hours else cache["status"],
            "stale": age_hours > stale_after_hours,
            "cache_age_hours": round(age_hours, 2),
            "data": cache,
        }
