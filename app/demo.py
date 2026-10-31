"""Run the integrated submission demo with an isolated simulator database."""

import argparse
import secrets
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import uvicorn

from app.main import Settings, create_app
from examples.backend_fixture import DissentFixtureProvider, FixtureProvider


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline", action="store_true", help="Explicit fixture agents, real rules and HITL"
    )
    parser.add_argument(
        "--third-turn", action="store_true", help="Inject one dissent; requires --offline"
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.third_turn and not args.offline:
        parser.error("--third-turn requires --offline so injected messages cannot look live")
    settings = Settings.from_env()
    if settings.session_token and len(settings.session_token) < 16:
        parser.error("Configure PLAB_SESSION_TOKEN with at least 16 characters in local .env")
    if not args.offline and not (settings.api_key and settings.model):
        parser.error("Configure a provider key/model or use --offline")
    if not settings.session_token:
        settings = replace(settings, session_token=secrets.token_urlsafe(24))
        print(
            "Local operator access code (this demo process only): " + settings.session_token,
            flush=True,
        )
    settings = replace(
        settings,
        db_path=Path("artifacts/demos") / f"{uuid4()}.sqlite3",
        public_origin=f"http://127.0.0.1:{args.port}",
    )
    provider = (
        (DissentFixtureProvider() if args.third_turn else FixtureProvider())
        if args.offline
        else None
    )
    uvicorn.run(create_app(settings=settings, provider=provider), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
