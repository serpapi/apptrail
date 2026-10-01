from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path

from argon2 import extract_parameters
from argon2.exceptions import InvalidHashError
from fastapi import HTTPException
from starlette.responses import JSONResponse

from .auth import USERNAME_PATTERN
from .db import SCHEMA_VERSION, Database, now

MAX_RESTORE_BYTES = 2 * 1024 * 1024 * 1024


class RestoreGate:
    def __init__(self):
        self.condition = threading.Condition()
        self.active = 0
        self.restoring = False
        self.operation = "A restore"

    @contextmanager
    def exclusive(self, operation="A restore"):
        with self.condition:
            if self.restoring:
                raise HTTPException(
                    503, f"{self.operation} is already in progress. Try again shortly."
                )
            self.restoring = True
            self.operation = operation
            if not self.condition.wait_for(lambda: self.active == 1, timeout=30):
                self.restoring = False
                raise HTTPException(503, "The server is busy. Try again shortly.")
        try:
            yield
        finally:
            with self.condition:
                self.restoring = False


class RestoreMiddleware:
    def __init__(self, app, gate):
        self.app, self.gate = app, gate

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)
        with self.gate.condition:
            blocked = self.gate.restoring
            if not blocked:
                self.gate.active += 1
        if blocked:
            response = JSONResponse(
                {"detail": f"{self.gate.operation} is in progress. Try again shortly."},
                status_code=503,
                headers={"Retry-After": "5", "Cache-Control": "no-store"},
            )
            return await response(scope, receive, send)
        try:
            await self.app(scope, receive, send)
        finally:
            with self.gate.condition:
                self.gate.active -= 1
                self.gate.condition.notify_all()


def schema(connection):
    # Ignore SQL formatting differences between SQLite and SQLAlchemy versions.
    return {
        (kind, name): " ".join(sql.split()).lower() if sql else None
        for kind, name, sql in connection.execute(
            "SELECT type, name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        )
    }


def prepare_restore(upload: Path, directory: Path) -> Path:
    with upload.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\0":
            raise ValueError("Upload an AppTrail SQLite backup (.sqlite3).")

    # Build a trusted schema so uploaded triggers or constraints never reach the server.
    staged = Database(directory)
    staged.close()
    try:
        with (
            closing(sqlite3.connect(upload.as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(staged.path)) as target,
        ):
            source.execute("PRAGMA trusted_schema=OFF")
            version = source.execute("PRAGMA user_version").fetchone()[0]
            # Schema 7 updates image comparisons without changing the table layout.
            if version not in {6, SCHEMA_VERSION}:
                raise ValueError(
                    "This backup uses an incompatible database format. "
                    "Use a backup from a compatible AppTrail version."
                )
            if schema(source) != schema(target):
                raise ValueError("This file is not a compatible AppTrail backup.")
            if source.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                raise ValueError("The backup is damaged. Upload a different SQLite backup.")

            target.execute("BEGIN")
            for (table,) in target.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall():
                columns = source.execute(f'PRAGMA table_info("{table}")').fetchall()
                for _, column, kind, *_ in columns:
                    if kind != "JSON":
                        continue
                    if source.execute(
                        f'SELECT 1 FROM "{table}" WHERE NOT json_valid("{column}") LIMIT 1'
                    ).fetchone():
                        raise ValueError("The backup contains invalid app data.")
                    if table != "settings":
                        expected = (
                            "array" if column in {"aliases", "responses", "changes"} else "object"
                        )
                        if source.execute(
                            f'SELECT 1 FROM "{table}" WHERE json_type("{column}") != ? LIMIT 1',
                            (expected,),
                        ).fetchone():
                            raise ValueError("The backup contains incompatible app data.")
                # Sessions must never be imported, even from a manually copied database.
                if table in {"login_sessions", "auth_limits"}:
                    continue
                rows = source.execute(f'SELECT * FROM "{table}"')
                placeholders = ",".join("?" for _ in columns)
                while batch := rows.fetchmany(1 if table == "listing_assets" else 100):
                    target.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', batch)

            if target.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("The backup contains broken app or history references.")
            owners = target.execute("SELECT id, username, password_hash FROM owner").fetchall()
            if len(owners) != 1 or owners[0][0] != 1 or not owners[0][1]:
                raise ValueError("The backup must contain an AppTrail owner account.")
            username = owners[0][1]
            if (
                not isinstance(username, str)
                or not re.fullmatch(USERNAME_PATTERN, username)
                or username != username.lower()
            ):
                raise ValueError("The backup contains an invalid account username.")
            try:
                parameters = extract_parameters(owners[0][2])
                if not (
                    1 <= parameters.time_cost <= 10
                    and 8 <= parameters.memory_cost <= 262144
                    and 1 <= parameters.parallelism <= 8
                ):
                    raise InvalidHashError
            except (InvalidHashError, TypeError):
                raise ValueError("The backup contains an invalid account password.") from None
            target.execute(
                "UPDATE runs SET status='queued', available_at=?, "
                "error='Interrupted; resuming after restore.' WHERE status='running'",
                (now(),),
            )
            target.commit()
        if version == 6:
            from .listing_history import upgrade_image_comparisons

            with staged.engine.begin() as connection:
                upgrade_image_comparisons(connection)
    except sqlite3.DatabaseError:
        raise ValueError("The backup is damaged or contains incompatible app data.") from None
    finally:
        staged.close()
    return staged.path
