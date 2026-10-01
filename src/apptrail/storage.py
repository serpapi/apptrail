from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from .db import Setting, clear_responses, now

RUN_TIME = "COALESCE(finished_at, started_at, created_at)"
FINISHED = "status NOT IN ('queued', 'running')"
RESPONSE_BYTES = (
    "CASE WHEN json_array_length(responses) > 0 THEN length(CAST(responses AS BLOB)) ELSE 0 END"
)


def removed_by_cleanup(session, kind, checked_at):
    setting = session.get(Setting, "storage_cleanup")
    metadata = setting.value if setting and isinstance(setting.value, dict) else {}
    cutoff = metadata.get(kind)
    return isinstance(cutoff, (int, float)) and checked_at < cutoff


def image_references(connection):
    from .listing_history import MEDIA_FIELDS

    connection.execute("CREATE TEMP TABLE image_references (id TEXT PRIMARY KEY, seen REAL)")
    fields = ", ".join(f"json_extract(data, '$.{field}')" for field in sorted(MEDIA_FIELDS))
    for checked_at, *values in connection.execute(
        f"SELECT checked_at, {fields} FROM listing_snapshots"
    ):
        refs = []
        for value in values:
            parsed = json.loads(value) if value else None
            refs.extend(parsed if isinstance(parsed, list) else [parsed])
        connection.executemany(
            "INSERT INTO image_references VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET seen=MAX(seen, excluded.seen)",
            [
                (ref["asset_id"], checked_at)
                for ref in refs
                if isinstance(ref, dict) and ref.get("asset_id")
            ],
        )


def response_usage(connection, cutoff=None):
    condition = f"{FINISHED} AND {RUN_TIME} < ?" if cutoff is not None else "1"
    params = (cutoff,) if cutoff is not None else ()
    return connection.execute(
        f"SELECT COALESCE(SUM(size), 0), COUNT(*) FROM ("
        f"SELECT id, {RESPONSE_BYTES} AS size FROM runs WHERE {condition} "
        f"UNION ALL SELECT run_id, {RESPONSE_BYTES} AS size FROM run_payloads "
        f"WHERE run_id IN (SELECT id FROM runs WHERE {condition})"
        ") WHERE size > 0",
        params + params,
    ).fetchone()


def image_usage(connection, cutoff=None):
    condition = "WHERE refs.seen IS NULL OR refs.seen < ?" if cutoff is not None else ""
    return connection.execute(
        "SELECT COALESCE(SUM(length(content)), 0), COUNT(*) FROM listing_assets "
        "LEFT JOIN image_references refs ON refs.id=listing_assets.id " + condition,
        (cutoff,) if cutoff is not None else (),
    ).fetchone()


def usage(db):
    timestamp = now()
    with closing(sqlite3.connect(db.path, timeout=30)) as connection:
        connection.execute("BEGIN")
        image_references(connection)
        categories = {}
        for kind, measure in (("responses", response_usage), ("images", image_usage)):
            size, count = measure(connection)
            categories[kind] = {
                "bytes": size,
                "count": count,
                "older_than": {
                    str(days): dict(
                        zip(("bytes", "count"), measure(connection, timestamp - days * 86400))
                    )
                    for days in (7, 30)
                },
            }
    database_bytes = db.path.stat().st_size
    wal = db.path.with_name(db.path.name + "-wal")
    try:
        journal_bytes = wal.stat().st_size
    except FileNotFoundError:
        journal_bytes = 0
    return {
        **categories,
        "database_bytes": database_bytes,
        "journal_bytes": journal_bytes,
        "total_bytes": database_bytes + journal_bytes,
    }


def cleanup(db, kind, days):
    # The caller drains requests and stops the worker before changing stored history.
    cutoff = now() - days * 86400
    db.close()
    with closing(sqlite3.connect(db.path, timeout=30)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if kind == "responses":
            size, count = response_usage(connection, cutoff)
            condition = f"{FINISHED} AND {RUN_TIME} < ?"
            clear_responses(connection, "cleanup", condition=condition, parameters=(cutoff,))
        else:
            image_references(connection)
            size, count = image_usage(connection, cutoff)
            connection.execute(
                "DELETE FROM listing_assets WHERE id NOT IN "
                "(SELECT id FROM image_references WHERE seen >= ?)",
                (cutoff,),
            )
        row = connection.execute(
            "SELECT value FROM settings WHERE key='storage_cleanup'"
        ).fetchone()
        metadata = json.loads(row[0]) if row else {}
        if not isinstance(metadata, dict):
            metadata = {}
        previous = metadata.get(kind, 0)
        metadata[kind] = max(cutoff, previous if isinstance(previous, (int, float)) else 0)
        connection.execute(
            "INSERT INTO settings VALUES ('storage_cleanup', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(metadata),),
        )
        connection.commit()
        compacted = True
        try:
            connection.execute("VACUUM")
            compacted = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        except sqlite3.DatabaseError:
            # Deletion is already committed; report that disk reclamation needs another attempt.
            compacted = False
    return {
        "removed_bytes": size,
        "removed_count": count,
        "compacted": compacted,
        "usage": usage(db),
    }
