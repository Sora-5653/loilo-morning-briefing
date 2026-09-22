from __future__ import annotations

from copy import deepcopy

from . import parser
from .cache import COMPONENTS, now_iso
from .errors import ClassroomError, RequestFailed, SchemaError


def unique(rows):
    result = {}
    for row in rows:
        if row["id"] in result and result[row["id"]] != row:
            raise SchemaError("Conflicting Classroom item versions")
        result[row["id"]] = row
    return list(result.values())


def diff(old, new, component, course_id):
    before = {row["id"]: row for row in old}
    after = {row["id"]: row for row in new}
    changes = []
    for item_id, row in after.items():
        if row != before.get(item_id):
            fields = sorted(k for k in row if row[k] != before.get(item_id, {}).get(k))
            changes.append({"component": component, "course_id": course_id, "id": item_id,
                            "kind": "updated" if item_id in before else "added", "changed_fields": fields})
    for item_id in before.keys() - after.keys():
        changes.append({"component": component, "course_id": course_id, "id": item_id,
                        "kind": "removed", "changed_fields": []})
    return changes


class ClassroomCollector:
    def __init__(self, client, cache):
        self.client, self.cache = client, cache

    def _assignments(self, course):
        rows = self.client.assignments(course["course_id"])
        submissions = {}
        for raw in self.client.submissions(course["course_id"]):
            raw = parser.object_value(raw)
            if raw.get("courseId") != course["course_id"]:
                raise SchemaError("Classroom submission course mismatch")
            key = parser.text(raw.get("courseWorkId"), required=True, limit=256)
            if key in submissions and submissions[key] != raw:
                raise SchemaError("Conflicting Classroom submissions")
            submissions[key] = raw
        return unique([parser.assignment(raw, course, submissions.get(parser.object_value(raw).get("id"))) for raw in rows])

    def run(self):
        try:
            old = self.cache.load()
        except SchemaError:
            old = None
        if old and old["account_key"] != self.client.account_key:
            old = None
        courses = [parser.course(raw) for raw in self.client.courses()]
        if len({row["course_id"] for row in courses}) != len(courses):
            raise SchemaError("Duplicate Classroom courses")
        now = now_iso()
        result = {"schema_version": 1, "account_key": self.client.account_key,
                  "fetched_at": now, "status": "success", "courses": courses,
                  "course_status": [], "errors": [], "changes": [],
                  **{key: [] for key in COMPONENTS}}
        successful = 0
        for course in courses:
            course_id = course["course_id"]
            for component in COMPONENTS:
                previous = [row for row in (old or {}).get(component, []) if row["course_id"] == course_id]
                prior_status = next((row for row in (old or {}).get("course_status", [])
                                     if row["course_id"] == course_id and row["component"] == component), {})
                try:
                    if component == "assignments":
                        fresh = self._assignments(course)
                    else:
                        raw = getattr(self.client, component)(course_id)
                        fresh = unique([parser.entry(row, course, component) for row in raw])
                    result[component].extend(fresh)
                    result["changes"].extend(diff(previous, fresh, component, course_id))
                    successful += 1
                    status = "success"
                    fetched_at = now
                except ClassroomError as exc:
                    result[component].extend(deepcopy(previous))
                    result["errors"].append({"course_id": course_id, "component": component, "error": type(exc).__name__})
                    status = "failed"
                    fetched_at = prior_status.get("last_successful_fetch_at")
                result["course_status"].append({"course_id": course_id, "course": course["course"],
                                                "component": component, "status": status,
                                                "last_successful_fetch_at": fetched_at})
        if courses and not successful:
            raise RequestFailed("All Classroom components failed")
        result["status"] = "partial" if result["errors"] else "success"
        self.cache.write(result)
        return result
