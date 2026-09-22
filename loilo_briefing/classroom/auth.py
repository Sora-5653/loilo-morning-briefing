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
            raise AuthenticationRequired("Classroom sign-in is required")
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
        raise AuthenticationRequired("Classroom credentials are unavailable") from exc


def authorize(client_path: Path) -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SetupRequired("Classroom dependencies are not installed") from exc
    try:
        config = json.loads(client_path.read_text(encoding="utf-8-sig"))
        installed = config["installed"]
        if installed["auth_uri"] != "https://accounts.google.com/o/oauth2/auth":
            raise ValueError("unexpected authorization endpoint")
        if installed["token_uri"] != TOKEN_URI:
            raise ValueError("unexpected token endpoint")
        flow = InstalledAppFlow.from_client_config(config, SCOPES, autogenerate_code_verifier=True)
        credentials = flow.run_local_server(
            host="localhost", port=0, timeout_seconds=300,
            authorization_prompt_message="Google Classroomの読み取り専用アクセスをブラウザーで許可してください。",
            success_message="認証が完了しました。この画面を閉じてください。",
            access_type="offline", prompt="consent",
        )
        granted = credentials.granted_scopes or credentials.scopes or []
        if not set(SCOPES).issubset(granted) or not credentials.refresh_token:
            raise ValueError("required read permissions were not granted")
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
        raise AuthenticationRequired("Classroom authorization did not complete") from exc
