from __future__ import annotations

import argparse
from pathlib import Path

from ..cli import _print
from .auth import authorize, load_credentials
from .cache import ClassroomCache
from .client import ClassroomClient
from .collector import ClassroomCollector
from .errors import AuthenticationRequired, ClassroomError, RequestFailed, SetupRequired


def main(argv=None):
    parser = argparse.ArgumentParser(prog="classroom-briefing")
    parser.add_argument("command", choices=["collect", "briefing-input", "auth-login", "auth-status"])
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--client-secret", type=Path)
    args = parser.parse_args(argv)
    cache = ClassroomCache(args.cache_dir)
    try:
        if args.command == "briefing-input":
            _print(cache.briefing_input())
        elif args.command == "auth-login":
            if not args.client_secret:
                parser.error("auth-login requires --client-secret")
            authorize(args.client_secret)
            _print({"source": "classroom", "status": "authorized"})
        elif args.command == "auth-status":
            load_credentials()
            _print({"source": "classroom", "status": "stored"})
        else:
            result = ClassroomCollector(ClassroomClient.connect(), cache).run()
            _print({"source": "classroom", "status": result["status"], "courses": len(result["courses"]),
                    "assignments": len(result["assignments"]), "changes": len(result["changes"]),
                    "errors": len(result["errors"])})
        return 0
    except Exception as exc:
        error = exc if isinstance(exc, ClassroomError) else RequestFailed("Classroom operation failed")
        if args.command == "collect":
            cache.failure(error)
        status = "authentication_required" if isinstance(error, AuthenticationRequired) else "setup_required" if isinstance(error, SetupRequired) else "failed"
        _print({"source": "classroom", "status": status, "error": type(error).__name__, "cache_preserved": cache.path.exists()})
        return 2 if isinstance(error, (AuthenticationRequired, SetupRequired)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
