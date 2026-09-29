from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .errors import AuthenticationRequired, SetupRequired

SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
]
VAULT_SERVICE = "Codex:DailyBrief:GoogleClassroom"
VAULT_USER = "oauth"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def _vault():
    if os.name != "nt":
        raise SetupRequired("Windows Credential Manager is required")
    try:
        from keyring.backends.Windows import WinVaultKeyring
    except ImportError as exc:
        raise SetupRequired("Classroom dependencies are not installed") from exc
    return WinVaultKeyring()


def load_credentials():
    try:
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise SetupRequired("Classroom dependencies are not installed") from exc
    try:
        stored = _vault().get_password(VAULT_SERVICE, VAULT_USER)
        if not stored:
            raise AuthenticationRequired("Classroom sign-in is required", reason="not_signed_in")
        record = json.loads(stored)
        info = record["credentials"]
        if set(info["scopes"]) != set(SCOPES) or info["token_uri"] != TOKEN_URI:
            raise ValueError("unexpected authorization")
        account_key = str(uuid.UUID(record["account_key"]))
        credentials = Credentials.from_authorized_user_info(info, SCOPES)
        if not credentials.refresh_token:
            raise ValueError("missing refresh token")
        return credentials, account_key
    except (AuthenticationRequired, SetupRequired):
        raise
    except Exception as exc:
        raise AuthenticationRequired("Classroom credentials are unavailable", reason="stored_credentials_invalid") from exc


def authorize(client_path: Path) -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SetupRequired("Classroom dependencies are not installed") from exc
    config = _client_config(client_path)
    try:
        flow = InstalledAppFlow.from_client_config(config, SCOPES, autogenerate_code_verifier=True)
        credentials = flow.run_local_server(
            host="localhost", port=0, timeout_seconds=300,
            authorization_prompt_message="Google Classroomの読み取り専用アクセスをブラウザーで許可してください。",
            success_message="認証が完了しました。この画面を閉じてください。",
            access_type="offline", prompt="consent",
        )
    except Exception as exc:
        # oauthlib raises a bare Warning when a checkbox was cleared on
        # Google's granular consent screen ("Scope has changed").
        reason = ("consent_denied" if type(exc).__name__ == "AccessDeniedError"
                  else "scopes_not_granted" if type(exc) is Warning
                  else "authorization_incomplete")
        raise AuthenticationRequired("Classroom authorization did not complete", reason=reason) from exc
    granted = credentials.granted_scopes or credentials.scopes or []
    if not set(SCOPES).issubset(granted):
        raise AuthenticationRequired("Classroom read permissions were not granted", reason="scopes_not_granted")
    if not credentials.refresh_token:
        raise AuthenticationRequired("Classroom refresh token was not issued", reason="refresh_token_missing")
    try:
        # Store refresh credentials only, never a plaintext token file.
        record = {
            "account_key": str(uuid.uuid4()),
            "credentials": {
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "refresh_token": credentials.refresh_token,
                "token_uri": TOKEN_URI,
                "scopes": SCOPES,
            },
        }
        _vault().set_password(VAULT_SERVICE, VAULT_USER, json.dumps(record))
    except SetupRequired:
        raise
    except Exception as exc:
        raise AuthenticationRequired("Classroom credentials could not be stored", reason="vault_write_failed") from exc


def _client_config(client_path: Path) -> dict:
    """Validate the downloaded OAuth client before opening the browser."""
    try:
        config = json.loads(Path(client_path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SetupRequired("OAuth client file is unreadable", reason="client_secret_unreadable") from exc
    installed = config.get("installed") if isinstance(config, dict) else None
    if not isinstance(installed, dict):
        # "web" clients reject the loopback redirect; a desktop client is required.
        raise SetupRequired("OAuth client is not a desktop app", reason="client_not_desktop")
    if (installed.get("auth_uri") != "https://accounts.google.com/o/oauth2/auth"
            or installed.get("token_uri") != TOKEN_URI):
        raise SetupRequired("OAuth client endpoints are unexpected", reason="client_endpoint_unexpected")
    if not all(isinstance(installed.get(k), str) and installed[k] for k in ("client_id", "client_secret")):
        raise SetupRequired("OAuth client is incomplete", reason="client_secret_unreadable")
    return config
