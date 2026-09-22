from __future__ import annotations

from datetime import date, datetime, timezone
from urllib.parse import urlparse

from ..parser import _safe_web_url
from .errors import SchemaError


def object_value(value):
    if not isinstance(value, dict):
        raise SchemaError("Expected a Classroom object")
    return value


def text(value, *, required=False, limit=30000):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise SchemaError("Invalid Classroom text")
    return value.strip()


def timestamp(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(text(value, required=True, limit=128).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("missing timezone")
    except ValueError as exc:
        raise SchemaError("Invalid Classroom timestamp") from exc
    return parsed.astimezone(timezone.utc).isoformat()


def link(value):
    safe = _safe_web_url(value)
    if safe and urlparse(safe).scheme == "https" and urlparse(safe).hostname == "classroom.google.com":
        return safe
    return None


def course(raw):
    raw = object_value(raw)
    return {"course_id": text(raw.get("id"), required=True, limit=256),
            "course": text(raw.get("name"), required=True, limit=2000),
            "url": link(raw.get("alternateLink"))}


def entry(raw, course, component):
    raw = object_value(raw)
    if raw.get("courseId") != course["course_id"]:
        raise SchemaError("Classroom course mismatch")
    return {
        "id": text(raw.get("id"), required=True, limit=256),
        "course_id": course["course_id"], "course": course["course"],
        "title": "" if component == "announcements" else text(raw.get("title"), required=True),
        "text": text(raw.get("text" if component == "announcements" else "description", "")),
        "created_at": timestamp(raw.get("creationTime")),
        "updated_at": timestamp(raw.get("updateTime")),
        "url": link(raw.get("alternateLink")),
    }


def assignment(raw, course, submission):
    result = entry(raw, course, "assignments")
    due_date = raw.get("dueDate")
    due_time = raw.get("dueTime")
    result["deadline"] = None
    if due_date is not None or due_time is not None:
        try:
            d, t = object_value(due_date), object_value(due_time)
            values = [d[k] for k in ("year", "month", "day")]
            values += [t.get(k, 0) for k in ("hours", "minutes", "seconds", "nanos")]
            if any(type(value) is not int for value in values) or not 0 <= values[-1] < 1_000_000_000:
                raise ValueError("invalid date fields")
            date(*values[:3])
            result["deadline"] = datetime(*values[:6], microsecond=values[-1] // 1000,
                                           tzinfo=timezone.utc).isoformat()
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaError("Invalid Classroom deadline") from exc
    submission = object_value(submission) if submission is not None else {}
    state = submission.get("state", "STATE_UNSPECIFIED")
    if not isinstance(state, str):
        raise SchemaError("Invalid Classroom submission state")
    late = submission.get("late", False) if submission else None
    if late is not None and not isinstance(late, bool):
        raise SchemaError("Invalid Classroom late flag")
    result.update({
        "status": {"NEW": "not_submitted", "CREATED": "not_submitted",
                   "RECLAIMED_BY_STUDENT": "not_submitted", "TURNED_IN": "submitted",
                   "RETURNED": "returned"}.get(state, "unknown"),
        "submission_state": state,
        "submission_updated_at": timestamp(submission.get("updateTime")),
        "late": late,
    })
    return result
