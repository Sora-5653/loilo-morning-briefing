from __future__ import annotations

import json
import re
import socket
import time
from dataclasses import dataclass
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .auth import COOKIE_NAME, InitialState, parse_initial_state
from .errors import (
    AuthenticationRequired,
    HttpError,
    NetworkError,
    SchemaError,
    UnsafeRequest,
)


DEFAULT_ORIGIN = "https://loilonote.app"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
SHARED_NOTE_PROTOCOL_VERSION = "0.4.0"
DOCUMENT_ASSET_HOST = "loilonote-assets.loilo.tv"
DOCUMENT_REDIRECT_QUERY_KEYS = {
    "Expires",
    "Key-Pair-Id",
    "Signature",
    "response-content-disposition",
    "response-content-type",
}
SHARED_NOTE_REDIRECT_QUERY_KEYS = DOCUMENT_REDIRECT_QUERY_KEYS | {
    "loilo_note_revision",
    "loilo_share_latest_protocol_version",
}
SHARED_NOTE_ASSET_PATH = re.compile(
    r"/note_[0-9]{1,10}/[A-Za-z0-9._-]{1,256}\.lnnote"
)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str


class Transport(Protocol):
    def request(
        self, method: str, url: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse: ...


class UrllibTransport:
    def __init__(self) -> None:
        self._opener = build_opener(_RejectRedirects())

    def request(
        self, method: str, url: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        request = Request(url, method=method, headers=dict(headers))
        try:
            with self._opener.open(request, timeout=timeout) as response:
                parsed = urlparse(url)
                is_document = (
                    "/api/documents/" in parsed.path
                    or (
                        parsed.path.startswith("/api/notes/")
                        and parsed.path.endswith("/snapshot")
                    )
                    or (
                        parsed.hostname == DOCUMENT_ASSET_HOST
                        and (
                            parsed.path.startswith("/document/")
                            or SHARED_NOTE_ASSET_PATH.fullmatch(parsed.path)
                        )
                    )
                )
                limit = MAX_DOCUMENT_BYTES if is_document else MAX_JSON_BYTES
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise SchemaError("LoiLoNote response exceeds the safety limit")
                return HttpResponse(
                    status=response.status,
                    headers=dict(response.headers.items()),
                    body=body,
                    url=response.geturl(),
                )
        except HTTPError as exc:
            body = exc.read(64 * 1024)
            return HttpResponse(
                status=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=body,
                url=exc.geturl(),
            )
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise NetworkError("LoiLoNote network request failed") from exc


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SessionBootstrap:
    def __init__(
        self,
        transport: Transport | None = None,
        origin: str = DEFAULT_ORIGIN,
        timeout: float = 20.0,
    ) -> None:
        self.transport = transport or UrllibTransport()
        self.origin = origin.rstrip("/")
        parsed = urlparse(self.origin)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "loilonote.app"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise UnsafeRequest("unsafe LoiLoNote bootstrap origin")
        self.timeout = timeout

    def fetch(self, session_cookie: str) -> InitialState:
        response = self.transport.request(
            "GET",
            f"{self.origin}/_/",
            {
                "Accept": "text/html,application/xhtml+xml",
                "Cookie": f"{COOKIE_NAME}={session_cookie}",
                "User-Agent": "LoiLoMorningBriefing/0.1",
            },
            self.timeout,
        )
        location = next(
            (value for key, value in response.headers.items() if key.lower() == "location"),
            "",
        )
        if (
            response.status in (401, 403)
            or "/login" in urlparse(response.url).path
            or (300 <= response.status < 400 and "/login" in urlparse(location).path)
        ):
            raise AuthenticationRequired("LoiLoNote authentication required")
        if response.status != 200:
            raise HttpError(response.status, "failed to refresh LoiLoNote session")
        try:
            text = response.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SchemaError("invalid LoiLoNote bootstrap encoding") from exc
        return parse_initial_state(text)


class LoiLoClient:
    """Strict read-only client for the endpoints used by the Web app.

    The public request layer only permits GET. The bearer-like auth token is
    appended at send time and is never returned by this class or included in
    exception messages.
    """

    def __init__(
        self,
        state: InitialState,
        session_cookie: str,
        transport: Transport | None = None,
        timeout: float = 20.0,
        retries: int = 2,
        backoff_seconds: float = 0.5,
    ) -> None:
        self.state = state
        self.session_cookie = session_cookie
        self.transport = transport or UrllibTransport()
        self.timeout = timeout
        self.retries = max(0, min(retries, 3))
        self.backoff_seconds = max(0.0, backoff_seconds)

    def _url(self, path: str, query: Mapping[str, object] | None = None) -> str:
        if not path.startswith("/api/"):
            raise UnsafeRequest("only LoiLoNote API paths are permitted")
        base = self.state.lns_endpoint + "/"
        url = urljoin(base, path.lstrip("/"))
        parsed = urlparse(url)
        base_host = urlparse(self.state.lns_endpoint).hostname
        if parsed.scheme != "https" or parsed.hostname != base_host:
            raise UnsafeRequest("cross-origin LoiLoNote request rejected")
        params = {str(k): str(v) for k, v in (query or {}).items() if v is not None}
        params["auth_token"] = self.state.auth_token
        return f"{url}?{urlencode(params)}"

    def _request_get(
        self,
        url: str,
        headers: Mapping[str, str],
        *,
        accept_document_redirect: bool = False,
    ) -> HttpResponse:
        last_network_error: NetworkError | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.transport.request("GET", url, headers, self.timeout)
            except NetworkError as exc:
                last_network_error = exc
                if attempt >= self.retries:
                    raise
                time.sleep(self.backoff_seconds * (2**attempt))
                continue
            if response.status in (401, 403):
                raise AuthenticationRequired("LoiLoNote authentication required")
            if accept_document_redirect and response.status in {301, 302, 303, 307, 308}:
                return response
            if response.status == 429 or 500 <= response.status <= 599:
                if attempt < self.retries:
                    time.sleep(self.backoff_seconds * (2**attempt))
                    continue
            if response.status < 200 or response.status >= 300:
                raise HttpError(response.status)
            return response
        raise last_network_error or NetworkError("LoiLoNote network request failed")

    def _get(
        self,
        path: str,
        query: Mapping[str, object] | None = None,
        *,
        accept_document_redirect: bool = False,
    ) -> HttpResponse:
        url = self._url(path, query)
        headers = {
            "Accept": "application/json,application/octet-stream",
            "Cookie": f"{COOKIE_NAME}={self.session_cookie}",
            "User-Agent": "LoiLoMorningBriefing/0.1",
        }
        return self._request_get(
            url, headers, accept_document_redirect=accept_document_redirect
        )

    def _get_json(self, path: str, query: Mapping[str, object] | None = None):
        response = self._get(path, query)
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaError("invalid JSON from LoiLoNote") from exc

    def get_courses(self):
        return self._get_json("/api/courses/v3")

    def get_submissions(
        self, course_id: str, *, limit: int = 30, next_page_key: str | None = None
    ):
        course_id = quote(str(course_id), safe="")
        query = {"limit": min(max(limit, 1), 100), "next_page_key": next_page_key}
        return self._get_json(f"/api/courses/{course_id}/submissions/v2", query)

    def get_submission_is_submitted(self, course_id: str, submission_number: str):
        course_id = quote(str(course_id), safe="")
        submission_number = quote(str(submission_number), safe="")
        return self._get_json(
            f"/api/courses/{course_id}/submissions/{submission_number}/submitted"
        )

    def get_timeline_entries(
        self,
        course_id: str,
        *,
        count: int = 20,
        after: str | None = None,
        before: str | None = None,
    ):
        course_id = quote(str(course_id), safe="")
        return self._get_json(
            f"/api/courses/{course_id}/timeline_entries",
            {
                "count": min(max(count, 1), 50),
                "after": after,
                "before": before,
                "omit_submission_related": "true",
            },
        )

    def get_shared_notes(
        self,
        course_id: str,
        *,
        limit: int = 100,
        next_page_key: str | None = None,
    ):
        return self._get_json(
            "/api/notes/shared/v3",
            {
                "course_id": str(course_id),
                "limit": min(max(limit, 1), 100),
                "next_page_key": next_page_key,
                "order_by": "update_time_desc",
            },
        )

    def get_note_group_notes(
        self,
        note_group_id: str,
        *,
        limit: int = 100,
        next_page_key: str | None = None,
    ):
        note_group_id = quote(str(note_group_id), safe="")
        return self._get_json(
            f"/api/note_groups/{note_group_id}/notes",
            {
                "limit": min(max(limit, 1), 100),
                "next_page_key": next_page_key,
                "order_by": "update_time_desc",
            },
        )

    def download_shared_note_snapshot(
        self,
        note_id: str,
        *,
        protocol_version: str = SHARED_NOTE_PROTOCOL_VERSION,
    ) -> bytes:
        note_id = quote(str(note_id), safe="")
        response = self._get(
            f"/api/notes/{note_id}/snapshot",
            {"protocol_version": protocol_version},
            accept_document_redirect=True,
        )
        if 200 <= response.status < 300:
            return response.body
        location = next(
            (value for key, value in response.headers.items() if key.lower() == "location"),
            "",
        )
        parsed = urlparse(location)
        query_keys = {key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        if (
            parsed.scheme != "https"
            or parsed.hostname != DOCUMENT_ASSET_HOST
            or parsed.port is not None
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not SHARED_NOTE_ASSET_PATH.fullmatch(parsed.path)
            or not {
                "Expires",
                "Key-Pair-Id",
                "Signature",
                "loilo_note_revision",
                "loilo_share_latest_protocol_version",
            }.issubset(query_keys)
            or not query_keys.issubset(SHARED_NOTE_REDIRECT_QUERY_KEYS)
        ):
            raise UnsafeRequest("unsafe LoiLoNote shared note redirect")
        asset_headers = {
            "Accept": "application/octet-stream",
            "User-Agent": "LoiLoMorningBriefing/0.1",
        }
        return self._request_get(location, asset_headers).body

    def get_share_work_config(self):
        return self._get_json("/api/lns_share_work")

    def download_document(self, document_id: str) -> bytes:
        document_id = quote(str(document_id), safe="")
        response = self._get(
            f"/api/documents/{document_id}", accept_document_redirect=True
        )
        if 200 <= response.status < 300:
            return response.body
        location = next(
            (value for key, value in response.headers.items() if key.lower() == "location"),
            "",
        )
        parsed = urlparse(location)
        query_keys = {key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        if (
            parsed.scheme != "https"
            or parsed.hostname != DOCUMENT_ASSET_HOST
            or parsed.port is not None
            or parsed.username
            or parsed.password
            or not parsed.path.startswith("/document/")
            or not {"Expires", "Key-Pair-Id", "Signature"}.issubset(query_keys)
            or not query_keys.issubset(DOCUMENT_REDIRECT_QUERY_KEYS)
        ):
            raise UnsafeRequest("unsafe LoiLoNote document redirect")
        asset_headers = {
            "Accept": "application/octet-stream",
            "User-Agent": "LoiLoMorningBriefing/0.1",
        }
        return self._request_get(location, asset_headers).body
