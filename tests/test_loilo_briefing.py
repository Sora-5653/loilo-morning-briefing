from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from loilo_briefing.auth import InitialState, normalize_session_cookie, parse_initial_state
from loilo_briefing.cache import CacheStore, SCHEMA_VERSION, validate_cache
from loilo_briefing.client import HttpResponse, LoiLoClient, SessionBootstrap
from loilo_briefing.cli import _print, build_parser
from loilo_briefing.collector import Collector
from loilo_briefing.errors import (
    AuthenticationRequired,
    CollectionFailed,
    HttpError,
    NetworkError,
    SchemaError,
    UnsafeRequest,
)
from loilo_briefing.parser import (
    MAX_DOCUMENT_CARDS,
    parse_assignment,
    parse_courses,
    parse_document_cards,
    parse_note_group_notes_page,
    parse_shared_note,
    parse_shared_notes_page,
    parse_submissions_page,
    parse_timeline_entry,
    parse_timeline_response,
    select_shared_note_protocol_version,
)


def document_bytes(frames, *, entire_body=True) -> bytes:
    body = {"version": "3.15.0.0", "format": "note", "data": {"frames": frames}}
    if entire_body:
        body = {"entire_body": body}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("body", json.dumps(body, ensure_ascii=False))
    return stream.getvalue()


def text_frame(text: str, card_id: str = "card-1"):
    return {
        "id": card_id,
        "type": "title",
        "content": {},
        "gadgets": {"title": {"type": "text", "text": text}},
    }


def valid_cache(fetched_at: str | None = None):
    return {
        "schema_version": SCHEMA_VERSION,
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
        "status": "success",
        "courses": [{"course_id": "1", "course": "現代文"}],
        "assignments": [],
        "timeline": [],
        "course_status": [],
        "errors": [],
    }


class QueueTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers), timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ParserTests(unittest.TestCase):
    def test_01_courses_nested_groups(self):
        payload = [
            {
                "user_group_id": 9,
                "user_group_name": "2-A",
                "courses": [
                    {"course_id": 1, "name": "現代文"},
                    {"course_id": 2, "name": "数学"},
                ],
            }
        ]
        self.assertEqual(["現代文", "数学"], [x["course"] for x in parse_courses(payload)])

    def test_02_courses_flat_shape(self):
        self.assertEqual(
            [{"course_id": "1", "course": "英語"}],
            parse_courses([{"course_id": 1, "subject_name": "英語"}]),
        )

    def test_03_course_schema_change_fails(self):
        with self.assertRaises(SchemaError):
            parse_courses([{"courses": [{"course_id": 1}]}])

    def test_04_assignment_deadline_and_submitted(self):
        item = parse_assignment(
            {
                "submission_number": 3,
                "message": "小論文",
                "expiry": "2026-08-15T23:59:00+09:00",
                "submitted": True,
                "timestamp": "2026-08-12T07:00:00+09:00",
            },
            {"course_id": "1", "course": "現代文"},
        )
        self.assertEqual("submitted", item["status"])
        self.assertEqual("2026-08-15T23:59:00+09:00", item["deadline"])

    def test_05_assignment_without_deadline(self):
        item = parse_assignment(
            {"submission_number": 3, "message": "任意課題", "expiry": None, "submitted": False},
            {"course_id": "1", "course": "現代文"},
        )
        self.assertIsNone(item["deadline"])
        self.assertEqual("not_submitted", item["status"])

    def test_06_assignment_status_lookup(self):
        item = parse_assignment(
            {"submission_number": 3, "message": "課題"},
            {"course_id": "1", "course": "現代文"},
            submitted_lookup=lambda _number: {"result": True},
        )
        self.assertEqual("submitted", item["status"])

    def test_07_submissions_page(self):
        items, key = parse_submissions_page({"submissions": [], "next_page_key": "next"})
        self.assertEqual([], items)
        self.assertEqual("next", key)

    def test_blank_assignment_title_is_preserved_without_inventing_text(self):
        for title in ("", " \t "):
            with self.subTest(title=title):
                item = parse_assignment(
                    {
                        "submission_number": 7,
                        "message": title,
                        "expiry": None,
                        "submitted": False,
                        "timestamp": "2026-09-13T07:00:00+09:00",
                    },
                    {"course_id": "1", "course": "現代文"},
                )
                self.assertEqual("", item["title"])
                self.assertEqual("7", item["id"])
                self.assertEqual("not_submitted", item["status"])
                self.assertIsNone(item["deadline"])

    def test_missing_or_invalid_assignment_title_still_fails(self):
        for fields in ({}, {"message": None}, {"message": 123}, {"message": "x" * 2001}):
            with self.subTest(fields=fields):
                with self.assertRaises(SchemaError):
                    parse_assignment(
                        {"submission_number": 7, **fields},
                        {"course_id": "1", "course": "現代文"},
                    )

    def test_briefing_json_uses_utf8_even_with_cp932_stdout(self):
        data = {"title": "連絡 🟥", "status": "success"}
        buffer = io.BytesIO()
        with io.TextIOWrapper(buffer, encoding="cp932") as stdout:
            with patch("sys.stdout", stdout):
                _print(data)
            stdout.flush()
            self.assertEqual(data, json.loads(buffer.getvalue().decode("utf-8")))

    def test_08_text_card_body(self):
        cards = parse_document_cards(document_bytes([text_frame("教科書を持参してください。")]))
        self.assertEqual("text", cards[0]["type"])
        self.assertEqual("教科書を持参してください。", cards[0]["text"])

    def test_08b_text_card_without_redundant_type_field(self):
        frame = text_frame("実データ形式の連絡本文")
        del frame["gadgets"]["title"]["type"]
        cards = parse_document_cards(document_bytes([frame]))
        self.assertEqual("text", cards[0]["type"])
        self.assertEqual("実データ形式の連絡本文", cards[0]["text"])

    def test_09_multiple_card_types(self):
        frames = [
            text_frame("連絡"),
            {"id": "p", "type": "picture", "content": {"asset": {"extension": ".png"}}, "gadgets": {}},
            {"id": "d", "type": "pdf", "content": {"asset": {"extension": ".pdf"}}, "gadgets": {}},
            {"id": "w", "type": "web", "content": {"uri": "https://example.com/a"}, "gadgets": {}},
            {"id": "q", "type": "quiz", "content": {}, "gadgets": {}},
        ]
        cards = parse_document_cards(document_bytes(frames))
        self.assertEqual(
            ["text", "image", "pdf/document", "web/link", "unknown"],
            [card["type"] for card in cards],
        )

    def test_10_sensitive_web_url_is_not_persisted(self):
        frame = {
            "id": "w",
            "type": "web",
            "content": {"uri": "https://example.com/?auth_token=secret"},
            "gadgets": {},
        }
        card = parse_document_cards(document_bytes([frame]))[0]
        self.assertNotIn("url", card)

    def test_11_invalid_document_zip(self):
        with self.assertRaises(SchemaError):
            parse_document_cards(b"not-a-zip")

    def test_12_invalid_document_json(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("body", "{")
        with self.assertRaises(SchemaError):
            parse_document_cards(stream.getvalue())

    def test_13_timeline_entry(self):
        raw = {
            "id": 123,
            "type": "share",
            "timestamp": "2026-08-12T15:30:00+09:00",
            "share": {
                "author": {"id": 7, "display_name": "教師"},
                "document": {"id": "doc-1", "page_count": 1},
                "shared_with": [99, 100],
            },
        }
        entry = parse_timeline_entry(
            raw,
            {"course_id": "1", "course": "現代文"},
            lambda _doc: document_bytes([text_frame("次回の持ち物")]),
        )
        self.assertEqual("教師", entry["sender"])
        self.assertNotIn("shared_with", entry)
        self.assertEqual("次回の持ち物", entry["cards"][0]["text"])

    def test_14_non_share_timeline_entry_ignored(self):
        self.assertIsNone(
            parse_timeline_entry(
                {"id": 1, "type": "assignment"},
                {"course_id": "1", "course": "現代文"},
                lambda _doc: b"",
            )
        )

    def test_15_empty_timeline(self):
        self.assertEqual([], parse_timeline_response({"entries": []}))

    def test_15b_shared_notes_page_separates_notes_and_groups(self):
        notes, group_ids, next_key = parse_shared_notes_page(
            {
                "entities": [
                    {
                        "entity_type": "notes",
                        "notes": {"id": 1, "name": "連絡", "is_deleted": False},
                    },
                    {
                        "entity_type": "notes",
                        "notes": {"id": 2, "name": "削除済み", "is_deleted": True},
                    },
                    {
                        "entity_type": "note_groups",
                        "note_groups": {"id": 3, "name": "資料"},
                    },
                ],
                "next_page_key": "next",
            }
        )
        self.assertEqual([1], [note["id"] for note in notes])
        self.assertEqual(["3"], group_ids)
        self.assertEqual("next", next_key)

    def test_15c_note_group_page_and_shared_note_cards(self):
        raw_notes, next_key = parse_note_group_notes_page(
            {
                "notes": [
                    {
                        "id": 7,
                        "name": "共同編集",
                        "created_at": "2026-08-19T10:00:00+09:00",
                        "updated_at": "2026-08-20T10:00:00+09:00",
                    }
                ],
                "next_page_key": None,
            }
        )
        note = parse_shared_note(
            raw_notes[0],
            {"course_id": "1", "course": "現代文"},
            lambda _note_id: document_bytes(
                [text_frame("共有ノートの本文")], entire_body=False
            ),
        )
        self.assertIsNone(next_key)
        self.assertEqual("共同編集", note["name"])
        self.assertEqual("共有ノートの本文", note["cards"][0]["text"])

    def test_15d_unknown_shared_note_entity_fails_closed(self):
        with self.assertRaises(SchemaError):
            parse_shared_notes_page(
                {"entities": [{"entity_type": "future_entity", "value": {}}]}
            )

    def test_15e_shared_note_protocol_uses_supported_minimum(self):
        self.assertEqual(
            "0.3.0",
            select_shared_note_protocol_version(
                {"latest_protocol_version": "0.3.0"}, "0.4.0"
            ),
        )
        self.assertEqual(
            "0.4.0",
            select_shared_note_protocol_version(
                {"latest_protocol_version": "0.5.0"}, "0.4.0"
            ),
        )

    def test_15f_document_card_count_is_bounded(self):
        frames = [text_frame("x", str(index)) for index in range(MAX_DOCUMENT_CARDS + 1)]
        with self.assertRaises(SchemaError):
            parse_document_cards(document_bytes(frames))


class ClientTests(unittest.TestCase):
    def test_16_parse_initial_state(self):
        payload = {
            "lnsEndpoint": "https://loilonote.app",
            "authToken": "token-value",
            "userInfo": {"user_id": 1},
        }
        encoded = base64.b64encode(quote(json.dumps(payload)).encode()).decode()
        state = parse_initial_state(f'<div id="initial-state" data-json="{encoded}"></div>')
        self.assertEqual("1", state.user_id)

    def test_16b_parse_initial_state_attribute_order_is_irrelevant(self):
        payload = {"lnsEndpoint": "https://loilonote.app", "authToken": "token-value"}
        encoded = base64.b64encode(quote(json.dumps(payload)).encode()).decode()
        state = parse_initial_state(f'<div data-json="{encoded}" class="x" id="initial-state"></div>')
        self.assertEqual("https://loilonote.app", state.lns_endpoint)

    def test_17_initial_state_rejects_external_endpoint(self):
        payload = {"lnsEndpoint": "https://evil.example", "authToken": "token"}
        encoded = base64.b64encode(quote(json.dumps(payload)).encode()).decode()
        with self.assertRaises(SchemaError):
            parse_initial_state(f'<div id="initial-state" data-json="{encoded}"></div>')

    def test_18_bootstrap_login_redirect_is_auth_required(self):
        transport = QueueTransport([HttpResponse(200, {}, b"login", "https://loilonote.app/login")])
        with self.assertRaises(AuthenticationRequired):
            SessionBootstrap(transport=transport).fetch("cookie")

    def test_18b_bootstrap_rejects_nonofficial_origin(self):
        with self.assertRaises(UnsafeRequest):
            SessionBootstrap(transport=QueueTransport([]), origin="https://evil.example")

    def test_19_client_uses_get_only(self):
        transport = QueueTransport([HttpResponse(200, {}, b"[]", "https://loilonote.app/api/courses/v3")])
        client = LoiLoClient(
            InitialState("https://loilonote.app", "secret"),
            "cookie",
            transport=transport,
            retries=0,
        )
        client.get_courses()
        self.assertEqual("GET", transport.calls[0][0])
        self.assertIn("auth_token=secret", transport.calls[0][1])

    def test_20_client_retries_network_error(self):
        transport = QueueTransport(
            [NetworkError("network"), HttpResponse(200, {}, b"[]", "https://loilonote.app/api/courses/v3")]
        )
        client = LoiLoClient(
            InitialState("https://loilonote.app", "secret"),
            "cookie",
            transport=transport,
            retries=1,
            backoff_seconds=0,
        )
        self.assertEqual([], client.get_courses())
        self.assertEqual(2, len(transport.calls))

    def test_21_client_retries_5xx(self):
        transport = QueueTransport(
            [
                HttpResponse(503, {}, b"", "https://loilonote.app/api/courses/v3"),
                HttpResponse(200, {}, b"[]", "https://loilonote.app/api/courses/v3"),
            ]
        )
        client = LoiLoClient(
            InitialState("https://loilonote.app", "secret"),
            "cookie",
            transport=transport,
            retries=1,
            backoff_seconds=0,
        )
        self.assertEqual([], client.get_courses())

    def test_22_client_does_not_retry_4xx(self):
        transport = QueueTransport([HttpResponse(404, {}, b"", "https://loilonote.app/api/courses/v3")])
        client = LoiLoClient(
            InitialState("https://loilonote.app", "secret"),
            "cookie",
            transport=transport,
            retries=2,
            backoff_seconds=0,
        )
        with self.assertRaises(HttpError):
            client.get_courses()
        self.assertEqual(1, len(transport.calls))

    def test_23_client_401_is_auth_required(self):
        transport = QueueTransport([HttpResponse(401, {}, b"", "https://loilonote.app/api/courses/v3")])
        client = LoiLoClient(
            InitialState("https://loilonote.app", "secret"),
            "cookie",
            transport=transport,
            retries=0,
        )
        with self.assertRaises(AuthenticationRequired):
            client.get_courses()

    def test_24_client_invalid_json(self):
        transport = QueueTransport([HttpResponse(200, {}, b"{", "https://loilonote.app/api/courses/v3")])
        client = LoiLoClient(
            InitialState("https://loilonote.app", "secret"),
            "cookie",
            transport=transport,
            retries=0,
        )
        with self.assertRaises(SchemaError):
            client.get_courses()

    def test_25_non_api_path_rejected(self):
        client = LoiLoClient(InitialState("https://loilonote.app", "secret"), "cookie")
        with self.assertRaises(UnsafeRequest):
            client._url("/login")

    def test_26_cookie_normalization(self):
        self.assertEqual("abc", normalize_session_cookie("connect.sid=abc"))

    def test_26b_path_ids_are_encoded(self):
        transport = QueueTransport([HttpResponse(200, {}, b'{"entries":[]}', "https://loilonote.app")])
        client = LoiLoClient(
            InitialState("https://loilonote.app", "secret"),
            "cookie",
            transport=transport,
            retries=0,
        )
        client.get_timeline_entries("../other?x=1")
        self.assertIn("..%2Fother%3Fx%3D1", transport.calls[0][1])

    def test_26c_document_follows_only_valid_asset_redirect_without_cookie(self):
        location = (
            "https://loilonote-assets.loilo.tv/document/a.lnfragment"
            "?Expires=1&Key-Pair-Id=k&Signature=s"
        )
        transport = QueueTransport(
            [
                HttpResponse(302, {"Location": location}, b"", "https://loilonote.app/api/documents/1"),
                HttpResponse(200, {}, b"document", location),
            ]
        )
        client = LoiLoClient(
            InitialState("https://loilonote.app", "secret"),
            "cookie",
            transport=transport,
            retries=0,
        )
        self.assertEqual(b"document", client.download_document("1"))
        self.assertNotIn("Cookie", transport.calls[1][2])
        self.assertNotIn("auth_token", transport.calls[1][1])

    def test_26d_document_rejects_external_redirect(self):
        location = "https://evil.example/document/a?Expires=1&Key-Pair-Id=k&Signature=s"
        transport = QueueTransport(
            [HttpResponse(302, {"Location": location}, b"", "https://loilonote.app/api/documents/1")]
        )
        client = LoiLoClient(
            InitialState("https://loilonote.app", "secret"),
            "cookie",
            transport=transport,
            retries=0,
        )
        with self.assertRaises(UnsafeRequest):
            client.download_document("1")
        with self.assertRaises(ValueError):
            normalize_session_cookie("abc; other=bad")

    def test_26e_shared_note_requests_are_get_only_and_bounded(self):
        transport = QueueTransport(
            [
                HttpResponse(200, {}, b'{"entities":[]}', "https://loilonote.app"),
                HttpResponse(200, {}, b'{"notes":[]}', "https://loilonote.app"),
                HttpResponse(200, {}, b"snapshot", "https://loilonote.app"),
            ]
        )
        client = LoiLoClient(
            InitialState("https://loilonote.app", "secret"),
            "cookie",
            transport=transport,
            retries=0,
        )
        client.get_shared_notes("1", limit=999)
        client.get_note_group_notes("../2", limit=999)
        self.assertEqual(b"snapshot", client.download_shared_note_snapshot("3"))
        self.assertTrue(all(call[0] == "GET" for call in transport.calls))
        self.assertIn("/api/notes/shared/v3?", transport.calls[0][1])
        self.assertIn("limit=100", transport.calls[0][1])
        self.assertIn("/api/note_groups/..%2F2/notes?", transport.calls[1][1])
        self.assertIn("/api/notes/3/snapshot?", transport.calls[2][1])
        self.assertIn("protocol_version=0.4.0", transport.calls[2][1])

    def test_26f_shared_note_follows_only_valid_asset_redirect_without_cookie(self):
        location = (
            "https://loilonote-assets.loilo.tv/note_21/note.lnnote"
            "?Expires=1&Key-Pair-Id=k&Signature=s&loilo_note_revision=4-2"
            "&loilo_share_latest_protocol_version=0.4.0"
        )
        transport = QueueTransport(
            [
                HttpResponse(302, {"Location": location}, b"", "https://loilonote.app"),
                HttpResponse(200, {}, b"snapshot", location),
            ]
        )
        client = LoiLoClient(
            InitialState("https://loilonote.app", "secret"),
            "cookie",
            transport=transport,
            retries=0,
        )
        self.assertEqual(b"snapshot", client.download_shared_note_snapshot("3"))
        self.assertNotIn("Cookie", transport.calls[1][2])
        self.assertNotIn("auth_token", transport.calls[1][1])

    def test_26g_shared_note_rejects_external_redirect(self):
        location = (
            "https://evil.example/note_21/note.lnnote"
            "?Expires=1&Key-Pair-Id=k&Signature=s"
        )
        transport = QueueTransport(
            [HttpResponse(302, {"Location": location}, b"", "https://loilonote.app")]
        )
        client = LoiLoClient(
            InitialState("https://loilonote.app", "secret"),
            "cookie",
            transport=transport,
            retries=0,
        )
        with self.assertRaises(UnsafeRequest):
            client.download_shared_note_snapshot("3")

    def test_26h_shared_note_rejects_malformed_asset_redirects(self):
        base = "https://loilonote-assets.loilo.tv"
        query = (
            "Expires=1&Key-Pair-Id=k&Signature=s&loilo_note_revision=4-2"
            "&loilo_share_latest_protocol_version=0.4.0"
        )
        locations = [
            f"{base}/document/note.lnnote?{query}",
            f"{base}/note_21/note.lnnote?Expires=1&Key-Pair-Id=k",
            f"{base}/note_21/note.lnnote?{query}&unexpected=1",
            f"{base}/note_21/note.lnnote?{query}#fragment",
        ]
        for location in locations:
            with self.subTest(location=location):
                transport = QueueTransport(
                    [
                        HttpResponse(
                            302, {"Location": location}, b"", "https://loilonote.app"
                        )
                    ]
                )
                client = LoiLoClient(
                    InitialState("https://loilonote.app", "secret"),
                    "cookie",
                    transport=transport,
                    retries=0,
                )
                with self.assertRaises(UnsafeRequest):
                    client.download_shared_note_snapshot("3")


class FakeApi:
    def __init__(self):
        self.fail_course = None
        self.courses = [
            {
                "user_group_id": 9,
                "user_group_name": "2-A",
                "courses": [
                    {"course_id": 1, "name": "現代文"},
                    {"course_id": 2, "name": "数学"},
                ],
            }
        ]
        self.submissions = {
            "1": [{"submission_number": 5, "message": "作文", "expiry": None, "submitted": False}],
            "2": [],
        }
        self.timelines = {
            "1": [
                {
                    "id": 101,
                    "type": "share",
                    "timestamp": "2026-08-12T15:30:00+09:00",
                    "share": {
                        "author": {"display_name": "先生"},
                        "document": {"id": "doc-1"},
                    },
                }
            ],
            "2": [],
        }
        self.documents = {"doc-1": document_bytes([text_frame("次回は資料集を持参")])}
        self.timeline_after = []
        self.shared_note_calls = []
        self.shared_note_snapshot_calls = []
        self.fail_shared_note_snapshot = None
        self.shared_notes = {
            "1": {
                "entities": [
                    {
                        "entity_type": "notes",
                        "notes": {
                            "id": 201,
                            "name": "授業の共有ノート",
                            "created_at": "2026-08-19T10:00:00+09:00",
                            "updated_at": "2026-08-20T10:00:00+09:00",
                        },
                    },
                    {
                        "entity_type": "note_groups",
                        "note_groups": {"id": 301, "name": "資料"},
                    },
                ],
                "next_page_key": None,
            },
            "2": {"entities": [], "next_page_key": None},
        }
        self.group_notes = {
            "301": {
                "notes": [
                    {
                        "id": 202,
                        "name": "フォルダー内の共有ノート",
                        "created_at": "2026-08-19T11:00:00+09:00",
                        "updated_at": "2026-08-20T11:00:00+09:00",
                    }
                ],
                "next_page_key": None,
            }
        }
        self.shared_note_documents = {
            "201": document_bytes([text_frame("共有ノートの連絡")], entire_body=False),
            "202": document_bytes([text_frame("フォルダー内の連絡")], entire_body=False),
        }

    def get_courses(self):
        return self.courses

    def get_submissions(self, course_id, *, next_page_key=None, limit=30):
        if self.fail_course == (str(course_id), "assignments"):
            raise NetworkError("failed")
        return {"submissions": self.submissions[str(course_id)], "next_page_key": None}

    def get_submission_is_submitted(self, course_id, number):
        return {"result": False}

    def get_timeline_entries(self, course_id, *, count=20, after=None, before=None):
        self.timeline_after.append((str(course_id), after))
        if self.fail_course == (str(course_id), "timeline"):
            raise NetworkError("failed")
        entries = self.timelines[str(course_id)]
        if after is not None:
            entries = [item for item in entries if int(item["id"]) > int(after)]
        return {"entries": entries}

    def download_document(self, document_id):
        return self.documents[document_id]

    def get_shared_notes(self, course_id, *, next_page_key=None, limit=100):
        self.shared_note_calls.append((str(course_id), next_page_key))
        if self.fail_course == (str(course_id), "shared_notes"):
            raise NetworkError("failed")
        return self.shared_notes[str(course_id)]

    def get_note_group_notes(self, note_group_id, *, next_page_key=None, limit=100):
        return self.group_notes[str(note_group_id)]

    def download_shared_note_snapshot(self, note_id, *, protocol_version="0.4.0"):
        self.shared_note_snapshot_calls.append(str(note_id))
        if self.fail_shared_note_snapshot == str(note_id):
            raise NetworkError("failed")
        return self.shared_note_documents[str(note_id)]

    def get_share_work_config(self):
        return {"latest_protocol_version": "0.4.0"}


class CacheAndCollectorTests(unittest.TestCase):
    def test_27_atomic_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CacheStore(Path(temp))
            store.write_cache(valid_cache())
            self.assertEqual("success", store.load_cache()["status"])
            self.assertFalse(any(Path(temp).glob("*.tmp")))

    def test_28_corrupt_cache_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CacheStore(Path(temp))
            store.directory.mkdir(parents=True, exist_ok=True)
            store.cache_path.write_text("{", encoding="utf-8")
            output = store.briefing_input()
            self.assertEqual("unavailable", output["status"])
            self.assertEqual("cache_corrupt", output["reason"])

    def test_29_stale_cache_is_marked(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CacheStore(Path(temp))
            old = datetime.now(timezone.utc) - timedelta(hours=48)
            store.write_cache(valid_cache(old.isoformat()))
            output = store.briefing_input(stale_after_hours=30)
            self.assertTrue(output["stale"])
            self.assertEqual("stale", output["status"])

    def test_30_collector_full_success_and_text(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CacheStore(Path(temp))
            result = Collector(FakeApi(), store).run()
            self.assertEqual("success", result["status"])
            self.assertEqual(2, len(result["courses"]))
            self.assertEqual("次回は資料集を持参", result["timeline"][0]["cards"][0]["text"])
            self.assertEqual("not_submitted", result["assignments"][0]["status"])

    def test_31_collector_cursor_prevents_duplicate_timeline(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CacheStore(Path(temp))
            api = FakeApi()
            Collector(api, store).run()
            second = Collector(api, store).run()
            self.assertEqual([], second["timeline"])
            self.assertEqual("101", api.timeline_after[-2][1])

    def test_blank_titles_do_not_fail_mixed_or_blank_only_courses(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeApi()
            blank = {"submission_number": 7, "message": "", "expiry": None, "submitted": False}
            api.submissions["1"].append(dict(blank))
            api.submissions["2"] = [dict(blank)]
            store = CacheStore(Path(temp))
            result = Collector(api, store).run()
            self.assertEqual("success", result["status"])
            self.assertEqual([], result["errors"])
            self.assertEqual(3, len(result["assignments"]))
            self.assertTrue(all(row["assignments"] == "success" for row in result["course_status"]))
            self.assertEqual(result, store.briefing_input()["data"])

    def test_32_partial_course_failure_uses_stale_slice(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CacheStore(Path(temp))
            api = FakeApi()
            Collector(api, store).run()
            api.fail_course = ("1", "timeline")
            result = Collector(api, store).run()
            self.assertEqual("partial", result["status"])
            self.assertEqual(1, len(result["timeline"]))
            course_status = next(x for x in result["course_status"] if x["course_id"] == "1")
            self.assertTrue(course_status["stale"])

    def test_33_all_components_fail_preserves_cache(self):
        class AllFail(FakeApi):
            def get_submissions(self, *args, **kwargs):
                raise NetworkError("failed")

            def get_timeline_entries(self, *args, **kwargs):
                raise NetworkError("failed")

        with tempfile.TemporaryDirectory() as temp:
            store = CacheStore(Path(temp))
            store.write_cache(valid_cache())
            before = store.cache_path.read_bytes()
            with self.assertRaises(CollectionFailed):
                Collector(AllFail(), store).run()
            self.assertEqual(before, store.cache_path.read_bytes())

    def test_34_empty_course_response_does_not_erase_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CacheStore(Path(temp))
            store.write_cache(valid_cache())
            api = FakeApi()
            api.courses = []
            before = store.cache_path.read_bytes()
            with self.assertRaises(CollectionFailed):
                Collector(api, store).run()
            self.assertEqual(before, store.cache_path.read_bytes())

    def test_35_cache_schema_change_fails(self):
        data = valid_cache()
        data["schema_version"] = 999
        with self.assertRaises(SchemaError):
            validate_cache(data)

    def test_36_source_has_no_browser_runtime_fallback(self):
        root = Path(__file__).resolve().parents[1] / "loilo_briefing"
        source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
        self.assertNotIn("playwright", source.lower())
        self.assertNotIn("selenium", source.lower())
        self.assertNotIn("computer use", source.lower())

    def test_37_shared_notes_are_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeApi()
            result = Collector(api, CacheStore(Path(temp))).run()
            self.assertNotIn("shared_notes", result)
            self.assertEqual([], api.shared_note_calls)

    def test_38_optional_shared_notes_include_root_and_group_cards(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeApi()
            store = CacheStore(Path(temp))
            result = Collector(api, store, include_shared_notes=True).run()
            self.assertEqual(2, len(result["shared_notes"]))
            texts = [note["cards"][0]["text"] for note in result["shared_notes"]]
            self.assertEqual(["共有ノートの連絡", "フォルダー内の連絡"], texts)
            self.assertEqual(["201", "202"], api.shared_note_snapshot_calls)
            self.assertEqual("success", result["course_status"][0]["shared_notes"])
            self.assertIn("shared_notes", store.load_cache())

    def test_39_unchanged_shared_notes_reuse_cached_cards(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeApi()
            store = CacheStore(Path(temp))
            Collector(api, store, include_shared_notes=True).run()
            Collector(api, store, include_shared_notes=True).run()
            self.assertEqual(["201", "202"], api.shared_note_snapshot_calls)

    def test_40_shared_note_failure_uses_stale_slice(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeApi()
            store = CacheStore(Path(temp))
            Collector(api, store, include_shared_notes=True).run()
            api.fail_course = ("1", "shared_notes")
            result = Collector(api, store, include_shared_notes=True).run()
            self.assertEqual("partial", result["status"])
            self.assertEqual(2, len(result["shared_notes"]))
            self.assertEqual("failed", result["course_status"][0]["shared_notes"])
            self.assertTrue(result["course_status"][0]["stale"])

    def test_41_collect_cli_accepts_shared_notes_option(self):
        args = build_parser().parse_args(["collect", "--include-shared-notes"])
        self.assertTrue(args.include_shared_notes)

    def test_42_failed_updated_snapshot_retains_stale_note(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeApi()
            store = CacheStore(Path(temp))
            Collector(api, store, include_shared_notes=True).run()
            api.shared_notes["1"]["entities"][0]["notes"]["updated_at"] = (
                "2026-08-20T12:00:00+09:00"
            )
            api.fail_shared_note_snapshot = "201"
            result = Collector(api, store, include_shared_notes=True).run()
            retained = next(note for note in result["shared_notes"] if note["id"] == "201")
            self.assertTrue(retained["stale"])
            self.assertEqual("partial", result["course_status"][0]["shared_notes"])

    def test_43_missing_change_markers_force_snapshot_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeApi()
            note = api.shared_notes["1"]["entities"][0]["notes"]
            note["updated_at"] = None
            note["metadata_updated_at"] = None
            store = CacheStore(Path(temp))
            Collector(api, store, include_shared_notes=True).run()
            Collector(api, store, include_shared_notes=True).run()
            self.assertEqual(["201", "202", "201"], api.shared_note_snapshot_calls)

    def test_44_shared_note_count_limit_fails_before_snapshots(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeApi()
            result = Collector(
                api,
                CacheStore(Path(temp)),
                include_shared_notes=True,
                max_shared_notes_per_course=1,
            ).run()
            self.assertEqual("failed", result["course_status"][0]["shared_notes"])
            self.assertEqual([], api.shared_note_snapshot_calls)

    def test_45_invalid_shared_note_cache_is_rejected(self):
        data = valid_cache()
        data["shared_notes"] = [
            {
                "id": "1",
                "course_id": "1",
                "name": "共有",
                "created_at": "not-a-time",
                "updated_at": None,
                "metadata_updated_at": None,
                "cards": [{"type": "text", "text": "本文"}],
            }
        ]
        with self.assertRaises(SchemaError):
            validate_cache(data)

    def test_46_failed_default_run_preserves_prior_optional_cache(self):
        class AllFail(FakeApi):
            def get_submissions(self, *args, **kwargs):
                raise NetworkError("failed")

            def get_timeline_entries(self, *args, **kwargs):
                raise NetworkError("failed")

        with tempfile.TemporaryDirectory() as temp:
            store = CacheStore(Path(temp))
            Collector(FakeApi(), store, include_shared_notes=True).run()
            before = store.cache_path.read_bytes()
            with self.assertRaises(CollectionFailed):
                Collector(AllFail(), store).run()
            self.assertEqual(before, store.cache_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
