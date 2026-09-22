from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from ..cache import _atomic_json_write
from .errors import SchemaError
from .parser import timestamp

COMPONENTS = ("assignments", "announcements", "materials")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def validate(data):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise SchemaError("Invalid Classroom cache schema")
    if data.get("status") not in {"success", "partial"} or not timestamp(data.get("fetched_at")):
        raise SchemaError("Invalid Classroom cache status")
    if not isinstance(data.get("account_key"), str):
        raise SchemaError("Invalid Classroom account key")
    for key in ("courses", "course_status", "errors", "changes", *COMPONENTS):
        if not isinstance(data.get(key), list):
            raise SchemaError("Invalid Classroom cache list")
    for row in data["courses"]:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ("course_id", "course")):
            raise SchemaError("Invalid Classroom cached course")
    for row in data["course_status"]:
        if (not isinstance(row, dict) or row.get("component") not in COMPONENTS
                or row.get("status") not in {"success", "failed"}
                or not isinstance(row.get("course_id"), str)):
            raise SchemaError("Invalid Classroom component status")
        timestamp(row.get("last_successful_fetch_at"))
    for row in data["changes"]:
        if (not isinstance(row, dict) or row.get("component") not in COMPONENTS
                or row.get("kind") not in {"added", "updated", "removed"}
                or not all(isinstance(row.get(k), str) for k in ("id", "course_id"))
                or not isinstance(row.get("changed_fields"), list)):
            raise SchemaError("Invalid Classroom cached change")
    for component in COMPONENTS:
        seen = set()
        for row in data[component]:
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ("id", "course_id", "course", "title", "text")):
                raise SchemaError("Invalid Classroom cached item")
            key = row["course_id"], row["id"]
            if key in seen:
                raise SchemaError("Duplicate Classroom cached item")
            seen.add(key)
            for name in ("created_at", "updated_at", "deadline", "submission_updated_at"):
                timestamp(row.get(name))
            if component == "assignments" and row.get("status") not in {"submitted", "not_submitted", "returned", "unknown"}:
                raise SchemaError("Invalid Classroom cached submission state")
    return data


class ClassroomCache:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ClassroomMorningBriefing"
        self.path = self.directory / "classroom-cache.json"
        self.attempt_path = self.directory / "classroom-attempt.json"

    def load(self):
        if not self.path.exists():
            return None
        try:
            return validate(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            raise SchemaError("Classroom cache is corrupt") from exc

    def write(self, data):
        _atomic_json_write(self.path, validate(data))

    def failure(self, error):
        _atomic_json_write(self.attempt_path, {"attempted_at": now_iso(), "error": type(error).__name__})

    def briefing_input(self, stale_after_hours=30):
        try:
            cache = self.load()
            attempt = json.loads(self.attempt_path.read_text(encoding="utf-8")) if self.attempt_path.exists() else None
            if attempt is not None and (not isinstance(attempt, dict) or not timestamp(attempt.get("attempted_at"))):
                raise SchemaError("Invalid Classroom attempt")
        except (SchemaError, OSError, ValueError, TypeError):
            return {"source": "classroom", "status": "unavailable", "reason": "cache_corrupt"}
        if cache is None:
            return {"source": "classroom", "status": "unavailable", "reason": "authentication_required_or_not_collected"}
        fetched = datetime.fromisoformat(timestamp(cache["fetched_at"]))
        age = max(0, (datetime.now(timezone.utc) - fetched).total_seconds() / 3600)
        failed = bool(attempt and datetime.fromisoformat(timestamp(attempt["attempted_at"])) >= fetched)
        data = deepcopy(cache)
        data.pop("account_key")
        # Keep all current assignments for approaching deadlines; only emit
        # new/updated announcements and materials, not the historical snapshot.
        for component in ("announcements", "materials"):
            changed = {(row["course_id"], row["id"]) for row in data["changes"]
                       if row["component"] == component and row["kind"] in {"added", "updated"}}
            data[component] = [row for row in data[component] if (row["course_id"], row["id"]) in changed]
        return {"source": "classroom", "status": "stale" if age > stale_after_hours or failed else cache["status"],
                "stale": age > stale_after_hours or failed, "last_attempt_failed": failed,
                "cache_age_hours": round(age, 2), "data": data}
