from __future__ import annotations

import io
import importlib.util
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from loilo_briefing.classroom.__main__ import main
from loilo_briefing.classroom import parser
from loilo_briefing.classroom.auth import SCOPES
from loilo_briefing.classroom.cache import ClassroomCache
from loilo_briefing.classroom.client import ClassroomClient
from loilo_briefing.classroom.collector import ClassroomCollector
from loilo_briefing.classroom.errors import AuthenticationRequired, RequestFailed, SchemaError


def item(item_id="w1", **fields):
    return {"id": item_id, "courseId": "c1", "title": "課題", "description": "連絡 🟥",
            "creationTime": "2026-09-01T00:00:00Z", "updateTime": "2026-09-01T00:00:00Z",
            "alternateLink": "https://classroom.google.com/c/c1/a/w1", **fields}


class FakeApi:
    account_key = "account-a"

    def __init__(self):
        self.fail = set()
        self.work = [item(dueDate={"year": 2026, "month": 9, "day": 14}, dueTime={"hours": 14})]
        self.submission = [{"courseId": "c1", "courseWorkId": "w1", "state": "NEW"}]
        self.announcement = [item("n1", text="持ち物のお知らせ")]
        self.material = [item("m1")]

    def courses(self):
        return [{"id": "c1", "name": "現代文"}]

    def _get(self, key, rows):
        if key in self.fail:
            raise RequestFailed("test outage")
        return deepcopy(rows)

    def assignments(self, course_id):
        return self._get("assignments", self.work)

    def submissions(self, course_id):
        return self._get("submissions", self.submission)

    def announcements(self, course_id):
        return self._get("announcements", self.announcement)

    def materials(self, course_id):
        return self._get("materials", self.material)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = ClassroomCache(self.temp.name)
        self.api = FakeApi()
        self.collector = ClassroomCollector(self.api, self.cache)

    def test_first_fetch_then_no_change(self):
        first = self.collector.run()
        self.assertEqual("success", first["status"])
        self.assertEqual(3, len(first["changes"]))
        second = self.collector.run()
        self.assertEqual([], second["changes"])
        report = self.cache.briefing_input()["data"]
        self.assertEqual([], report["announcements"])
        self.assertEqual([], report["materials"])
        self.assertEqual(1, len(report["assignments"]))
        self.assertNotIn("account_key", report)

    def test_submission_only_change_and_returned_is_not_resubmission(self):
        self.collector.run()
        for state, expected in [("TURNED_IN", "submitted"), ("RETURNED", "returned"),
                                ("RECLAIMED_BY_STUDENT", "not_submitted")]:
            self.api.submission[0]["state"] = state
            result = self.collector.run()
            self.assertEqual(expected, result["assignments"][0]["status"])
            self.assertEqual(["assignments"], [row["component"] for row in result["changes"]])

    def test_same_timestamp_edit_is_detected(self):
        self.collector.run()
        self.api.announcement[0]["text"] = "訂正のお知らせ"
        result = self.collector.run()
        self.assertEqual("updated", result["changes"][0]["kind"])
        self.assertIn("text", result["changes"][0]["changed_fields"])

    def test_deadline_change_and_removal(self):
        self.collector.run()
        self.api.work[0]["dueTime"]["hours"] = 15
        self.api.material = []
        result = self.collector.run()
        self.assertEqual(["updated", "removed"], [row["kind"] for row in result["changes"]])
        self.assertIn("deadline", result["changes"][0]["changed_fields"])

    def test_partial_failure_retains_slice_then_recovers_missed_update(self):
        old = self.collector.run()
        self.api.announcement[0]["text"] = "障害中の連絡"
        self.api.fail.add("announcements")
        partial = self.collector.run()
        self.assertEqual("partial", partial["status"])
        self.assertEqual(old["announcements"], partial["announcements"])
        status = next(row for row in partial["course_status"] if row["component"] == "announcements")
        self.assertEqual(old["fetched_at"], status["last_successful_fetch_at"])
        self.api.fail.clear()
        recovered = self.collector.run()
        self.assertEqual("updated", recovered["changes"][0]["kind"])
        self.assertEqual("障害中の連絡", self.cache.briefing_input()["data"]["announcements"][0]["text"])

    def test_submission_failure_does_not_claim_fresh_status(self):
        original = self.collector.run()
        self.api.submission[0]["state"] = "TURNED_IN"
        self.api.fail.add("submissions")
        partial = self.collector.run()
        self.assertEqual(original["assignments"], partial["assignments"])
        self.assertEqual("failed", partial["course_status"][0]["status"])

    def test_full_failure_preserves_cache_and_reader_marks_failed_attempt_stale(self):
        self.collector.run()
        before = self.cache.path.read_bytes()
        self.api.fail = {"assignments", "announcements", "materials"}
        with self.assertRaises(RequestFailed) as error:
            self.collector.run()
        self.cache.failure(error.exception)
        self.assertEqual(before, self.cache.path.read_bytes())
        self.assertEqual("stale", self.cache.briefing_input()["status"])
        self.api.fail.clear()
        self.collector.run()
        self.assertEqual("success", self.cache.briefing_input()["status"])

    def test_invalid_row_retains_entire_component(self):
        original = self.collector.run()
        self.api.work.append({"id": "bad"})
        result = self.collector.run()
        self.assertEqual("partial", result["status"])
        self.assertEqual(original["assignments"], result["assignments"])

    def test_missing_submission_is_unknown(self):
        self.api.submission = []
        result = self.collector.run()["assignments"][0]
        self.assertEqual("unknown", result["status"])
        self.assertIsNone(result["late"])

    def test_empty_courses_and_account_change(self):
        self.collector.run()
        self.api.account_key = "account-b"
        result = self.collector.run()
        self.assertTrue(all(row["kind"] == "added" for row in result["changes"]))
        self.api.courses = lambda: []
        empty = self.collector.run()
        self.assertEqual("success", empty["status"])
        self.assertEqual([], empty["assignments"])

    def test_old_cache_and_corrupt_cache(self):
        result = self.collector.run()
        result["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
        self.cache.write(result)
        self.assertEqual("stale", self.cache.briefing_input()["status"])
        self.cache.path.write_text("{", encoding="utf-8")
        self.assertEqual("unavailable", self.cache.briefing_input()["status"])

    def test_cli_without_authorization_preserves_cache_and_sanitizes_errors(self):
        self.collector.run()
        before = self.cache.path.read_bytes()
        with patch("loilo_briefing.classroom.__main__.ClassroomClient.connect", side_effect=AuthenticationRequired("secret")), patch("sys.stdout", new_callable=io.StringIO) as output:
            code = main(["collect", "--cache-dir", self.temp.name])
        self.assertEqual(2, code)
        self.assertNotIn("secret", output.getvalue())
        self.assertEqual(before, self.cache.path.read_bytes())


class ParserTests(unittest.TestCase):
    course = {"course_id": "c1", "course": "現代文"}

    def test_utc_deadline_and_missing_time(self):
        raw = item(dueDate={"year": 2026, "month": 9, "day": 14}, dueTime={})
        self.assertEqual("2026-09-14T00:00:00+00:00", parser.assignment(raw, self.course, None)["deadline"])
        del raw["dueTime"]
        with self.assertRaises(SchemaError):
            parser.assignment(raw, self.course, None)

    def test_private_fields_and_unsafe_links_not_persisted(self):
        raw = item(userId="other-student", assignedGrade=90, alternateLink="https://classroom.google.com/?token=secret")
        parsed = parser.assignment(raw, self.course, {"state": "RETURNED", "userId": "private", "assignedGrade": 90})
        self.assertNotIn("userId", parsed)
        self.assertNotIn("assignedGrade", parsed)
        self.assertIsNone(parsed["url"])


class Endpoint:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(method="GET", execute=lambda **_: next(self.responses))


class ClientTests(unittest.TestCase):
    def test_pagination_uses_next_token_even_for_short_pages(self):
        endpoint = Endpoint([{"courses": [{"id": "one"}], "nextPageToken": "next"}, {}])
        client = ClassroomClient(None, "a")
        self.assertEqual([{"id": "one"}], client._list(endpoint, "courses", "id"))
        self.assertEqual("next", endpoint.calls[1]["pageToken"])

    def test_bad_pagination_or_structure_is_not_success(self):
        for responses in [[{"courses": [], "nextPageToken": "same"}] * 2,
                          [{"courses": [], "nextPageToken": False}],
                          [{"unexpected": []}], [{"courses": {}}]]:
            with self.subTest(responses=responses), self.assertRaises(SchemaError):
                ClassroomClient(None, "a")._list(Endpoint(responses), "courses", "id")

    def test_safety_limit_does_not_return_partial_page_as_success(self):
        with self.assertRaises(SchemaError):
            ClassroomClient(None, "a", max_pages=1)._list(Endpoint([{"courses": [], "nextPageToken": "more"}]), "courses", "id")

    def test_only_readonly_scopes_are_requested(self):
        self.assertEqual(4, len(SCOPES))
        self.assertTrue(all(scope.endswith(".readonly") for scope in SCOPES))
        self.assertNotIn("https://www.googleapis.com/auth/classroom.coursework.students.readonly", SCOPES)


@unittest.skipUnless(importlib.util.find_spec("googleapiclient"), "Install the classroom extra for SDK verification")
class SdkTests(unittest.TestCase):
    def test_official_sdk_requests_and_material_response_shape(self):
        from googleapiclient.discovery import build
        from httplib2 import Response

        calls = []
        bodies = iter([{"courses": [{"id": "c1", "name": "現代文"}]},
                       {"courseWork": [item()]},
                       {"studentSubmissions": [{"courseWorkId": "w1", "state": "NEW"}]},
                       {"announcements": [item("n1")]},
                       {"courseWorkMaterial": [item("m1")]}])

        class Http:
            def request(self, uri, method="GET", body=None, headers=None, **kwargs):
                calls.append((uri, method, body))
                return Response({"status": "200"}), json.dumps(next(bodies)).encode()

        service = build("classroom", "v1", http=Http(), static_discovery=True, cache_discovery=False)
        client = ClassroomClient(service, "a")
        self.assertEqual("c1", client.courses()[0]["id"])
        self.assertEqual("w1", client.assignments("c1")[0]["id"])
        self.assertEqual("NEW", client.submissions("c1")[0]["state"])
        self.assertEqual("n1", client.announcements("c1")[0]["id"])
        self.assertEqual("m1", client.materials("c1")[0]["id"])
        self.assertTrue(all(method == "GET" and body is None for _, method, body in calls))
        self.assertEqual(["me"], parse_qs(urlparse(calls[0][0]).query)["studentId"])
        query = parse_qs(urlparse(calls[2][0]).query)
        self.assertEqual(["me"], query["userId"])
        self.assertNotIn("assignedGrade", query["fields"][0])
        self.assertNotIn("userId", query["fields"][0])


if __name__ == "__main__":
    unittest.main()
