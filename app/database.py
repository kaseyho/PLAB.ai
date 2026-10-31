"""The same short, serialized transactions on SQLite and shared Postgres."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)


class Cursor:
    def __init__(self, raw: sqlite3.Cursor | psycopg.Cursor[dict[str, Any]]) -> None:
        self.raw = raw

    def fetchone(self) -> Any:
        row = self.raw.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.raw.fetchall()]


class Connection:
    def __init__(self, raw: sqlite3.Connection | psycopg.Connection[dict[str, Any]]) -> None:
        self.raw = raw

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> Cursor:
        if isinstance(self.raw, sqlite3.Connection):
            return Cursor(self.raw.execute(sql, parameters))
        # Only application-owned SQL reaches this adapter. Values stay bound parameters.
        return Cursor(self.raw.execute(sql.replace("?", "%s"), parameters))

    def executescript(self, schema: str) -> None:
        # The schema consists solely of CREATE TABLE statements.
        for statement in schema.split(";"):
            if statement.strip():
                self.execute(statement)


@contextmanager
def transaction(path: Path, database_url: str = "") -> Iterator[Connection]:
    if database_url:
        with psycopg.connect(database_url, connect_timeout=5, row_factory=dict_row) as db:
            db.execute("SET LOCAL lock_timeout = '3s'")
            # Match BEGIN IMMEDIATE across serverless instances, including approvals.
            db.execute("SELECT pg_advisory_xact_lock(73421002)")
            yield Connection(db)
        return
    local = sqlite3.connect(path, timeout=1.0, isolation_level=None)
    local.row_factory = sqlite3.Row
    try:
        local.execute("BEGIN IMMEDIATE")
        yield Connection(local)
        local.commit()
    except BaseException:
        local.rollback()
        raise
    finally:
        local.close()
