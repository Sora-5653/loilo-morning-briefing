from __future__ import annotations

import base64
import ctypes
import getpass
import html
import json
import os
from ctypes import wintypes
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote, urlparse

from .errors import AuthenticationRequired, SchemaError


VAULT_TARGET = "Codex:LoiLoNote:connect.sid"
COOKIE_NAME = "connect.sid"


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _advapi32():
    if os.name != "nt":
        raise RuntimeError("Windows Credential Manager is required")
    dll = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    dll.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    dll.CredWriteW.restype = wintypes.BOOL
    dll.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
    ]
    dll.CredReadW.restype = wintypes.BOOL
    dll.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    dll.CredDeleteW.restype = wintypes.BOOL
    dll.CredFree.argtypes = [ctypes.c_void_p]
    return dll


def normalize_session_cookie(value: str) -> str:
    value = value.strip()
    if value.lower().startswith(f"{COOKIE_NAME.lower()}="):
        value = value.split("=", 1)[1]
    if not value or any(ch in value for ch in "\r\n;"):
        raise ValueError("invalid LoiLoNote session cookie")
    if len(value.encode("utf-8")) > 2400:
        raise ValueError("LoiLoNote session cookie is unexpectedly large")
    return value


def store_session_cookie(value: str) -> None:
    value = normalize_session_cookie(value)
    blob = value.encode("utf-8")
    buf = ctypes.create_string_buffer(blob)
    credential = _CREDENTIALW()
    credential.Type = 1  # CRED_TYPE_GENERIC
    credential.TargetName = VAULT_TARGET
    credential.Comment = "LoiLoNote read-only collector session"
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE, scoped to this Windows user
    credential.UserName = "LoiLoNote session"
    dll = _advapi32()
    if not dll.CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def read_session_cookie() -> str:
    dll = _advapi32()
    ptr = ctypes.POINTER(_CREDENTIALW)()
    if not dll.CredReadW(VAULT_TARGET, 1, 0, ctypes.byref(ptr)):
        error = ctypes.get_last_error()
        if error == 1168:  # ERROR_NOT_FOUND
            raise AuthenticationRequired("LoiLoNote authentication required")
        raise ctypes.WinError(error)
    try:
        credential = ptr.contents
        raw = ctypes.string_at(
            credential.CredentialBlob, credential.CredentialBlobSize
        )
        return normalize_session_cookie(raw.decode("utf-8"))
    finally:
        dll.CredFree(ptr)


def delete_session_cookie() -> bool:
    dll = _advapi32()
    if dll.CredDeleteW(VAULT_TARGET, 1, 0):
        return True
    error = ctypes.get_last_error()
    if error == 1168:
        return False
    raise ctypes.WinError(error)


def prompt_and_store_session() -> None:
    value = getpass.getpass("LoiLoNote connect.sid (input is hidden): ")
    store_session_cookie(value)


@dataclass(frozen=True)
class InitialState:
    lns_endpoint: str
    auth_token: str
    user_id: str | None = None


class _InitialStateParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.encoded: str | None = None

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs}
        if values.get("id") == "initial-state" and isinstance(values.get("data-json"), str):
            self.encoded = values["data-json"]


def parse_initial_state(page_html: str) -> InitialState:
    parser = _InitialStateParser()
    parser.feed(page_html)
    if parser.encoded is None:
        raise AuthenticationRequired("LoiLoNote authentication required")
    try:
        encoded = html.unescape(parser.encoded)
        decoded = unquote(base64.b64decode(encoded, validate=True).decode("utf-8"))
        payload = json.loads(decoded)
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise SchemaError("invalid LoiLoNote initial-state payload") from exc
    if not isinstance(payload, dict):
        raise SchemaError("LoiLoNote initial-state must be an object")
    endpoint = payload.get("lnsEndpoint")
    token = payload.get("authToken")
    if not isinstance(endpoint, str) or not isinstance(token, str) or not token:
        raise SchemaError("LoiLoNote initial-state is missing endpoint or token")
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    allowed = (
        host == "loilonote.app"
        or host.endswith(".loilonote.app")
        or host == "loilo.tv"
        or host.endswith(".loilo.tv")
    )
    if parsed.scheme != "https" or not allowed or parsed.username or parsed.password:
        raise SchemaError("unsafe LoiLoNote API endpoint")
    if len(token) > 8192:
        raise SchemaError("LoiLoNote auth token is unexpectedly large")
    user_info = payload.get("userInfo")
    user_id = None
    if isinstance(user_info, dict) and isinstance(user_info.get("user_id"), (str, int)):
        user_id = str(user_info["user_id"])
    return InitialState(endpoint.rstrip("/"), token, user_id)
