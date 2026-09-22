from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .auth import (
    delete_session_cookie,
    prompt_and_store_session,
    read_session_cookie,
    store_session_cookie,
)
from .cache import CacheStore
from .client import LoiLoClient, SessionBootstrap
from .collector import Collector
from .errors import AuthenticationRequired, LoiLoError


def _print(data) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _cache(args) -> CacheStore:
    return CacheStore(Path(args.cache_dir) if args.cache_dir else None)


def command_collect(args) -> int:
    cache = _cache(args)
    try:
        cookie = read_session_cookie()
        state = SessionBootstrap(origin=args.origin).fetch(cookie)
        client = LoiLoClient(state, cookie)
        result = Collector(
            client, cache, include_shared_notes=args.include_shared_notes
        ).run()
        summary = {
            "source": "loilonote",
            "status": result["status"],
            "fetched_at": result["fetched_at"],
            "courses": len(result["courses"]),
            "assignments": len(result["assignments"]),
            "timeline": len(result["timeline"]),
            "cache_path": str(cache.cache_path),
        }
        if args.include_shared_notes:
            summary["shared_notes"] = len(result["shared_notes"])
        _print(summary)
        return 0
    except AuthenticationRequired:
        _print(
            {
                "source": "loilonote",
                "status": "authentication_required",
                "message": "LoiLoNote authentication required",
                "cache_preserved": cache.cache_path.exists(),
            }
        )
        return 2
    except LoiLoError as exc:
        _print(
            {
                "source": "loilonote",
                "status": "failed",
                "error": type(exc).__name__,
                "cache_preserved": cache.cache_path.exists(),
            }
        )
        return 1


def command_briefing_input(args) -> int:
    _print(_cache(args).briefing_input(args.stale_after_hours))
    return 0


def command_auth_store(args) -> int:
    if args.stdin:
        value = sys.stdin.readline()
        if not value:
            raise SystemExit("no session cookie received on stdin")
        store_session_cookie(value)
    else:
        prompt_and_store_session()
    _print({"status": "stored", "target": "Windows Credential Manager"})
    return 0


def command_auth_status(_args) -> int:
    try:
        read_session_cookie()
    except AuthenticationRequired:
        _print({"status": "missing"})
        return 1
    _print({"status": "stored", "target": "Windows Credential Manager"})
    return 0


def command_auth_clear(_args) -> int:
    _print({"status": "deleted" if delete_session_cookie() else "already_missing"})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loilo-briefing")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="refresh the read-only LoiLoNote cache")
    collect.add_argument("--cache-dir")
    collect.add_argument("--origin", default="https://loilonote.app")
    collect.add_argument(
        "--include-shared-notes",
        action="store_true",
        help="also extract cards from notes in each course's Shared Notes list",
    )
    collect.set_defaults(func=command_collect)

    briefing = sub.add_parser(
        "briefing-input", help="emit normalized cache data for the morning briefing"
    )
    briefing.add_argument("--cache-dir")
    briefing.add_argument("--stale-after-hours", type=float, default=30.0)
    briefing.set_defaults(func=command_briefing_input)

    auth = sub.add_parser("auth", help="manage the encrypted LoiLoNote session")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    store = auth_sub.add_parser("store-session")
    store.add_argument("--stdin", action="store_true")
    store.set_defaults(func=command_auth_store)
    status = auth_sub.add_parser("status")
    status.set_defaults(func=command_auth_status)
    clear = auth_sub.add_parser("clear")
    clear.set_defaults(func=command_auth_clear)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
