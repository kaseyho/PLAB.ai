"""Vercel's ASGI entrypoint; persistent state is provided by DATABASE_URL."""

from app.main import create_app

app = create_app()
