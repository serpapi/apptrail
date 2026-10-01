from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

SCHEMA_VERSION = 7


def now() -> float:
    return datetime.now(UTC).timestamp()


def private_sqlite_file(path: Path):
    # SQLite inherits database permissions when creating its journal files.
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            path.chmod(0o600)
    finally:
        os.close(descriptor)


class Base(DeclarativeBase):
    pass


class App(Base):
    __tablename__ = "apps"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    website: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("platform", "external_id"),
        UniqueConstraint("app_id", "platform"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(200))
    bundle_id: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    icon: Mapped[str] = mapped_column(Text, default="")
    developer: Mapped[str] = mapped_column(Text, default="")
    country: Mapped[str] = mapped_column(String(2), default="us")
    language: Mapped[str] = mapped_column(String(16), default="en")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    verified_at: Mapped[float] = mapped_column(Float, default=now)
    next_refresh_at: Mapped[float] = mapped_column(Float, default=lambda: now() + 86400)


class Competitor(Base):
    __tablename__ = "competitors"
    __table_args__ = (CheckConstraint("app_id != competitor_id"),)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), primary_key=True)
    competitor_id: Mapped[int] = mapped_column(
        ForeignKey("apps.id", ondelete="CASCADE"), primary_key=True
    )


class Candidate(Base):
    __tablename__ = "candidates"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[float] = mapped_column(Float)


class Monitor(Base):
    __tablename__ = "monitors"
    id: Mapped[int] = mapped_column(primary_key=True)
    signature: Mapped[str] = mapped_column(String(64), unique=True)
    query: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(40))
    country: Mapped[str] = mapped_column(String(2), default="us")
    language: Mapped[str] = mapped_column(String(16), default="en")
    device: Mapped[str] = mapped_column(String(20), default="")
    depth: Mapped[int] = mapped_column(Integer, default=1)
    frequency: Mapped[str] = mapped_column(String(20), default="daily")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[float] = mapped_column(Float, default=now)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Target(Base):
    __tablename__ = "targets"
    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), primary_key=True
    )
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), primary_key=True)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int | None] = mapped_column(
        ForeignKey("monitors.id", ondelete="SET NULL"), index=True
    )
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id", ondelete="SET NULL"))
    watch_id: Mapped[int | None] = mapped_column(
        ForeignKey("listing_watches.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(20), default="search")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    legacy_result: Mapped[dict] = mapped_column("result", JSON, default=dict, deferred=True)
    legacy_responses: Mapped[list] = mapped_column("responses", JSON, default=list, deferred=True)
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    requests_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=now, index=True)
    available_at: Mapped[float] = mapped_column(Float, default=now)
    started_at: Mapped[float | None] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[RunPayload | None] = relationship(
        cascade="all, delete-orphan", single_parent=True, passive_deletes=True
    )

    @property
    def result(self):
        return self.payload.result if self.payload else {}

    @result.setter
    def result(self, value):
        if self.payload is None:
            self.payload = RunPayload(result=value, responses=[])
        else:
            self.payload.result = value

    @property
    def responses(self):
        return self.payload.responses if self.payload else []

    @responses.setter
    def responses(self, value):
        if self.payload is None:
            self.payload = RunPayload(result={}, responses=value)
        else:
            self.payload.responses = value


class RunPayload(Base):
    __tablename__ = "run_payloads"
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    responses: Mapped[list] = mapped_column(JSON, default=list, deferred=True)


class Observation(Base):
    __tablename__ = "observations"
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    retrospective: Mapped[bool] = mapped_column(Boolean, default=False)


class Profile(Base):
    __tablename__ = "profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    checked_at: Mapped[float] = mapped_column(Float, default=now)
    data: Mapped[dict] = mapped_column(JSON)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


def backup_omits(session, key, item_id):
    setting = session.get(Setting, "backup_omissions")
    metadata = setting.value if setting and isinstance(setting.value, dict) else {}
    cutoff = metadata.get(key)
    return isinstance(cutoff, int) and item_id <= cutoff


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"))
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    created_at: Mapped[float] = mapped_column(Float, default=now)
    data: Mapped[dict] = mapped_column(JSON)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)


class RankAlertState(Base):
    __tablename__ = "rank_alert_states"
    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), primary_key=True
    )
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ListingWatch(Base):
    __tablename__ = "listing_watches"
    __table_args__ = (UniqueConstraint("listing_id", "country", "language"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    country: Mapped[str] = mapped_column(String(2))
    language: Mapped[str] = mapped_column(String(16))
    frequency: Mapped[str] = mapped_column(String(20), default="weekly")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    next_run_at: Mapped[float] = mapped_column(Float, default=now)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class ListingSnapshot(Base):
    __tablename__ = "listing_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(
        ForeignKey("listing_watches.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), unique=True)
    checked_at: Mapped[float] = mapped_column(Float, default=now)
    data: Mapped[dict] = mapped_column(JSON)
    changes: Mapped[list] = mapped_column(JSON, default=list)
    baseline: Mapped[bool] = mapped_column(Boolean, default=False)


class ListingAsset(Base):
    __tablename__ = "listing_assets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mime: Mapped[str] = mapped_column(String(40))
    content: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)


class Owner(Base):
    __tablename__ = "owner"
    __table_args__ = (CheckConstraint("id = 1"),)
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    username: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    process_id: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[float] = mapped_column(Float)
    last_seen: Mapped[float] = mapped_column(Float)


class AuthLimit(Base):
    __tablename__ = "auth_limits"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    started_at: Mapped[float] = mapped_column(Float)
    attempts: Mapped[int] = mapped_column(Integer)


class Database:
    def __init__(self, directory: Path):
        self.path = directory / "apptrail.sqlite3"
        private_sqlite_file(self.path)
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                Path(str(self.path) + suffix).chmod(0o600)
            except FileNotFoundError:
                pass
        self.engine = create_engine(
            f"sqlite:///{self.path}", connect_args={"check_same_thread": False, "timeout": 30}
        )

        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")

        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            version = connection.execute(text("PRAGMA user_version")).scalar()
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    "This database needs a newer AppTrail version. Upgrade AppTrail."
                )
            if version and version < SCHEMA_VERSION:
                backups = directory / "backups"
                backups.mkdir(exist_ok=True, mode=0o700)
                with tempfile.NamedTemporaryFile(
                    dir=backups, suffix=".sqlite3", delete=False
                ) as handle:
                    snapshot = Path(handle.name)
                try:
                    self.backup(snapshot)
                    snapshot.replace(backups / f"before-schema-{version}.sqlite3")
                finally:
                    snapshot.unlink(missing_ok=True)
            if version == 0:
                initial_schema = (
                    Path(__file__).parent / "migrations" / "001_initial.sql"
                ).read_text()
                for statement in initial_schema.split(";"):
                    if statement.strip():
                        connection.exec_driver_sql(statement)
                connection.execute(text("PRAGMA user_version=1"))
                version = 1
            if version == 1:
                for column in ("monitor_id", "listing_id"):
                    connection.execute(
                        text(
                            f"UPDATE runs SET status='cancelled', finished_at=:finished, "
                            "error='Duplicate pending check removed during upgrade.' "
                            "WHERE status IN ('queued', 'running') "
                            f"AND {column} IS NOT NULL AND id NOT IN "
                            f"(SELECT MIN(id) FROM runs WHERE {column} IS NOT NULL "
                            f"AND status IN ('queued', 'running') GROUP BY {column})"
                        ),
                        {"finished": now()},
                    )
                # A partial unique index also protects against concurrent HTTP enqueue requests.
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS one_active_search ON runs(monitor_id) WHERE status IN ('queued', 'running') AND monitor_id IS NOT NULL"
                    )
                )
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS one_active_profile ON runs(listing_id) WHERE status IN ('queued', 'running') AND listing_id IS NOT NULL"
                    )
                )
                connection.execute(text("PRAGMA user_version=2"))
                version = 2
            if version == 2:
                for table in (Owner.__table__, LoginSession.__table__, AuthLimit.__table__):
                    table.create(connection, checkfirst=True)
                connection.execute(text("PRAGMA user_version=3"))
                version = 3
            if version == 3:
                RunPayload.__table__.create(connection, checkfirst=True)
                connection.execute(
                    text(
                        "INSERT OR IGNORE INTO run_payloads (run_id, result, responses) "
                        "SELECT id, result, responses FROM runs "
                        "WHERE result != '{}' OR responses != '[]'"
                    )
                )
                # Keep empty legacy columns so the upgrade works with older SQLite libraries.
                connection.execute(text("UPDATE runs SET result='{}', responses='[]'"))
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_runs_checked_at ON runs(COALESCE(started_at, created_at))"
                    )
                )
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_runs_monitor_latest ON runs(monitor_id, created_at DESC, id DESC, status)"
                    )
                )
                connection.execute(text("PRAGMA user_version=4"))
                version = 4
            if version == 4:
                for model in (
                    Notification,
                    RankAlertState,
                    ListingWatch,
                    ListingSnapshot,
                    ListingAsset,
                ):
                    model.__table__.create(connection, checkfirst=True)
                # Some recovery tests start from an older version with newer columns intact.
                columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(runs)")}
                if "watch_id" not in columns:
                    connection.exec_driver_sql(
                        "ALTER TABLE runs ADD COLUMN watch_id INTEGER REFERENCES listing_watches(id) ON DELETE SET NULL"
                    )
                connection.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS one_active_listing_watch ON runs(watch_id) WHERE status IN ('queued', 'running') AND watch_id IS NOT NULL"
                )
                connection.execute(text("PRAGMA user_version=5"))
                version = 5
            if version == 5:
                Competitor.__table__.create(connection, checkfirst=True)
                connection.execute(text("PRAGMA user_version=6"))
                version = 6
            if version == 6:
                from .listing_history import upgrade_image_comparisons

                upgrade_image_comparisons(connection)
                connection.execute(text("PRAGMA user_version=7"))
        self.path.chmod(0o600)
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def backup(self, destination: Path, *, include_sessions=True, compact=False):
        private_sqlite_file(destination)
        with (
            closing(sqlite3.connect(self.path)) as source,
            closing(sqlite3.connect(destination)) as target,
        ):
            source.backup(target)
            if not include_sessions:
                target.execute("DELETE FROM login_sessions")
            if compact:
                target.execute("DELETE FROM listing_assets")
                target.execute("UPDATE run_payloads SET responses='[]'")
                target.execute("UPDATE runs SET responses='[]'")
                omissions = {
                    "responses_through_run": target.execute(
                        "SELECT COALESCE(MAX(id), 0) FROM runs"
                    ).fetchone()[0],
                    "images_through_snapshot": target.execute(
                        "SELECT COALESCE(MAX(id), 0) FROM listing_snapshots"
                    ).fetchone()[0],
                }
                target.execute(
                    "INSERT INTO settings (key, value) VALUES ('backup_omissions', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(omissions),),
                )
            target.commit()
            if compact:
                # Deleting rows alone leaves their pages in the downloaded file.
                target.execute("VACUUM")
        destination.chmod(0o600)

    def close(self):
        self.engine.dispose()

    def restore(self, source_path: Path):
        # Call only after HTTP requests and the worker have finished using the database.
        self.engine.dispose()
        with (
            closing(sqlite3.connect(source_path)) as source,
            closing(sqlite3.connect(self.path)) as target,
        ):
            source.backup(target)
