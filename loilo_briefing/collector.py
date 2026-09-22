from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .cache import SCHEMA_VERSION, CacheStore
from .client import SHARED_NOTE_PROTOCOL_VERSION
from .errors import CollectionFailed, LoiLoError, SchemaError
from .parser import (
    parse_assignment,
    parse_courses,
    parse_note_group_notes_page,
    parse_shared_note,
    parse_shared_notes_page,
    select_shared_note_protocol_version,
    parse_submissions_page,
    parse_timeline_entry,
    parse_timeline_response,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _merge_by_id(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {(item.get("course_id"), item.get("id")): item for item in old}
    merged.update({(item.get("course_id"), item.get("id")): item for item in new})
    return list(merged.values())


def _course_old_items(
    cache: dict[str, Any] | None, key: str, course_id: str
) -> list[dict[str, Any]]:
    if not cache:
        return []
    return [
        deepcopy(item)
        for item in cache.get(key, [])
        if item.get("course_id") == course_id
    ]


class Collector:
    def __init__(
        self,
        client,
        cache: CacheStore,
        *,
        timeline_count: int = 20,
        max_submission_pages: int = 5,
        include_shared_notes: bool = False,
        max_shared_note_pages: int = 5,
        max_shared_note_groups: int = 100,
        max_shared_notes_per_course: int = 1000,
        allow_empty_courses: bool = False,
    ) -> None:
        self.client = client
        self.cache = cache
        self.timeline_count = min(max(timeline_count, 1), 50)
        self.max_submission_pages = min(max(max_submission_pages, 1), 10)
        self.include_shared_notes = include_shared_notes
        self.max_shared_note_pages = min(max(max_shared_note_pages, 1), 10)
        self.max_shared_note_groups = min(max(max_shared_note_groups, 1), 500)
        self.max_shared_notes_per_course = min(
            max(max_shared_notes_per_course, 1), 5000
        )
        self.allow_empty_courses = allow_empty_courses

    def _collect_assignments(
        self, course: dict[str, str]
    ) -> tuple[list[dict[str, Any]], int]:
        raw_items: list[Any] = []
        next_key: str | None = None
        for _ in range(self.max_submission_pages):
            payload = self.client.get_submissions(
                course["course_id"], next_page_key=next_key
            )
            items, next_key = parse_submissions_page(payload)
            raw_items.extend(items)
            if not next_key:
                break
        else:
            if next_key:
                raise SchemaError("submission pagination exceeded the safety limit")

        def submitted_lookup(number: str):
            return self.client.get_submission_is_submitted(course["course_id"], number)

        parsed: list[dict[str, Any]] = []
        skipped = 0
        for item in raw_items:
            try:
                parsed.append(
                    parse_assignment(
                        item, course, submitted_lookup=submitted_lookup
                    )
                )
            except SchemaError:
                skipped += 1
        if raw_items and not parsed:
            raise SchemaError("all submissions in a course were invalid")
        return parsed, skipped

    def _collect_timeline(
        self, course: dict[str, str], after: str | None
    ) -> tuple[list[dict[str, Any]], str | None, int]:
        payload = self.client.get_timeline_entries(
            course["course_id"], count=self.timeline_count, after=after
        )
        raw_entries = parse_timeline_response(payload)
        entries: list[dict[str, Any]] = []
        ids: list[str] = []
        skipped = 0
        share_count = 0
        for raw in raw_entries:
            if isinstance(raw, dict) and raw.get("type") == "share":
                share_count += 1
            try:
                parsed = parse_timeline_entry(
                    raw, course, self.client.download_document
                )
            except LoiLoError:
                skipped += 1
                continue
            if parsed is not None:
                entries.append(parsed)
                ids.append(parsed["id"])
        if share_count and not entries:
            raise SchemaError("all timeline shares in a course were invalid")
        last_id = after
        if ids and not skipped:
            try:
                last_id = str(max(int(value) for value in ids))
            except ValueError:
                last_id = ids[-1]
        return entries, last_id, skipped

    def _collect_shared_notes(
        self,
        course: dict[str, str],
        old_cache: dict[str, Any] | None,
        protocol_version: str,
    ) -> tuple[list[dict[str, Any]], int]:
        raw_by_id: dict[str, dict[str, Any]] = {}
        skipped = 0
        saw_notes = False

        def add_notes(notes: list[dict[str, Any]]) -> None:
            nonlocal saw_notes, skipped
            saw_notes = saw_notes or bool(notes)
            for raw in notes:
                raw_id = raw.get("id") if isinstance(raw, dict) else None
                if (
                    not isinstance(raw_id, (str, int))
                    or isinstance(raw_id, bool)
                    or not str(raw_id)
                ):
                    skipped += 1
                    continue
                raw_by_id[str(raw_id)] = raw
                if len(raw_by_id) > self.max_shared_notes_per_course:
                    raise SchemaError("shared note count exceeds the safety limit")

        group_ids: list[str] = []
        next_key: str | None = None
        for _ in range(self.max_shared_note_pages):
            payload = self.client.get_shared_notes(
                course["course_id"], next_page_key=next_key
            )
            notes, groups, next_key = parse_shared_notes_page(payload)
            add_notes(notes)
            group_ids.extend(groups)
            if not next_key:
                break
        else:
            if next_key:
                raise SchemaError("shared note pagination exceeded the safety limit")

        group_ids = list(dict.fromkeys(group_ids))
        if len(group_ids) > self.max_shared_note_groups:
            raise SchemaError("shared note group count exceeds the safety limit")
        for group_id in group_ids:
            next_key = None
            for _ in range(self.max_shared_note_pages):
                payload = self.client.get_note_group_notes(
                    group_id, next_page_key=next_key
                )
                notes, next_key = parse_note_group_notes_page(payload)
                add_notes(notes)
                if not next_key:
                    break
            else:
                if next_key:
                    raise SchemaError(
                        "shared note group pagination exceeded the safety limit"
                    )

        old_by_id = {
            item.get("id"): item
            for item in _course_old_items(old_cache, "shared_notes", course["course_id"])
        }
        notes_by_id: dict[str, dict[str, Any]] = {}
        for raw_id, raw in raw_by_id.items():
            cached = old_by_id.get(raw_id)
            try:
                has_change_marker = (
                    raw.get("updated_at") is not None
                    or raw.get("metadata_updated_at") is not None
                )
                unchanged = (
                    cached is not None
                    and has_change_marker
                    and cached.get("updated_at") == raw.get("updated_at")
                    and cached.get("metadata_updated_at")
                    == raw.get("metadata_updated_at")
                )
                if unchanged:
                    parsed = deepcopy(cached)
                    parsed.pop("stale", None)
                else:
                    parsed = parse_shared_note(
                        raw,
                        course,
                        lambda note_id: self.client.download_shared_note_snapshot(
                            note_id, protocol_version=protocol_version
                        ),
                    )
            except (LoiLoError, TypeError, ValueError):
                skipped += 1
                if cached is not None:
                    parsed = deepcopy(cached)
                    parsed["stale"] = True
                    notes_by_id[raw_id] = parsed
                continue
            notes_by_id[parsed["id"]] = parsed
        if saw_notes and not notes_by_id:
            raise SchemaError("all shared notes in a course were invalid")
        return list(notes_by_id.values()), skipped

    def run(self) -> dict[str, Any]:
        try:
            old_cache = self.cache.load_cache()
        except SchemaError:
            old_cache = None
        try:
            state = self.cache.load_state()
        except SchemaError:
            state = {"schema_version": SCHEMA_VERSION, "courses": {}}

        raw_courses = self.client.get_courses()
        courses = parse_courses(raw_courses)
        if (
            not courses
            and old_cache
            and old_cache.get("courses")
            and not self.allow_empty_courses
        ):
            raise CollectionFailed(
                "empty course response rejected to protect the existing cache"
            )

        assignments: list[dict[str, Any]] = []
        timeline: list[dict[str, Any]] = []
        shared_notes: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        course_status: list[dict[str, Any]] = []
        successful_components = 0
        new_state = deepcopy(state)
        now = _now_iso()
        shared_note_protocol: str | None = None
        shared_note_protocol_error: LoiLoError | None = None
        if self.include_shared_notes:
            try:
                shared_note_protocol = select_shared_note_protocol_version(
                    self.client.get_share_work_config(), SHARED_NOTE_PROTOCOL_VERSION
                )
            except LoiLoError as exc:
                shared_note_protocol_error = exc

        for course in courses:
            course_id = course["course_id"]
            prior = state.get("courses", {}).get(course_id, {})
            status = {
                "course_id": course_id,
                "course": course["course"],
                "assignments": "failed",
                "timeline": "failed",
            }
            if self.include_shared_notes:
                status["shared_notes"] = "failed"
            try:
                fresh, skipped = self._collect_assignments(course)
                assignments.extend(fresh)
                status["assignments"] = "partial" if skipped else "success"
                successful_components += 1
                if skipped:
                    errors.append(
                        {
                            "course_id": course_id,
                            "component": "assignments:item",
                            "error": "SchemaError",
                        }
                    )
            except LoiLoError as exc:
                assignments.extend(_course_old_items(old_cache, "assignments", course_id))
                errors.append(
                    {
                        "course_id": course_id,
                        "component": "assignments",
                        "error": type(exc).__name__,
                    }
                )
            if self.include_shared_notes:
                try:
                    if shared_note_protocol_error is not None:
                        raise shared_note_protocol_error
                    if shared_note_protocol is None:
                        raise SchemaError("shared note protocol version is unavailable")
                    fresh_shared_notes, skipped = self._collect_shared_notes(
                        course, old_cache, shared_note_protocol
                    )
                    shared_notes.extend(fresh_shared_notes)
                    status["shared_notes"] = "partial" if skipped else "success"
                    successful_components += 1
                    if skipped:
                        errors.append(
                            {
                                "course_id": course_id,
                                "component": "shared_notes:item",
                                "error": "LoiLoError",
                            }
                        )
                    course_state = new_state.setdefault("courses", {}).setdefault(
                        course_id, {}
                    )
                    course_state["last_successful_shared_note_fetch_at"] = now
                except LoiLoError as exc:
                    shared_notes.extend(
                        _course_old_items(old_cache, "shared_notes", course_id)
                    )
                    errors.append(
                        {
                            "course_id": course_id,
                            "component": "shared_notes",
                            "error": type(exc).__name__,
                        }
                    )
            try:
                fresh_timeline, last_id, skipped = self._collect_timeline(
                    course, prior.get("last_seen_timeline_id")
                )
                timeline.extend(fresh_timeline)
                status["timeline"] = "partial" if skipped else "success"
                successful_components += 1
                if skipped:
                    errors.append(
                        {
                            "course_id": course_id,
                            "component": "timeline:item",
                            "error": "LoiLoError",
                        }
                    )
                course_state = new_state.setdefault("courses", {}).setdefault(course_id, {})
                course_state["last_seen_timeline_id"] = last_id
                course_state["last_successful_timeline_fetch_at"] = now
            except LoiLoError as exc:
                timeline.extend(_course_old_items(old_cache, "timeline", course_id))
                errors.append(
                    {
                        "course_id": course_id,
                        "component": "timeline",
                        "error": type(exc).__name__,
                    }
                )
            if status["assignments"] in {"success", "partial"}:
                course_state = new_state.setdefault("courses", {}).setdefault(course_id, {})
                course_state["last_successful_assignment_fetch_at"] = now
            components = ["assignments", "timeline"]
            if self.include_shared_notes:
                components.append("shared_notes")
            status["stale"] = any(status[key] == "failed" for key in components)
            course_status.append(status)

        if courses and successful_components == 0:
            raise CollectionFailed("all LoiLoNote course requests failed")

        result = {
            "schema_version": SCHEMA_VERSION,
            "fetched_at": now,
            "status": "partial" if errors else "success",
            "courses": courses,
            "assignments": _merge_by_id([], assignments),
            "timeline": _merge_by_id([], timeline),
            "course_status": course_status,
            "errors": errors,
        }
        if self.include_shared_notes:
            result["shared_notes"] = _merge_by_id([], shared_notes)
        self.cache.write_cache(result)
        new_state["last_run_at"] = now
        if not errors:
            new_state["last_successful_fetch_at"] = now
        self.cache.write_state(new_state)
        return result
