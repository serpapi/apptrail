from __future__ import annotations

import csv
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import __version__, storage
from .auth import Auth
from .backups import MAX_RESTORE_BYTES, RestoreGate, RestoreMiddleware, prepare_restore
from .config import Config
from .db import (
    App,
    Competitor,
    Database,
    Listing,
    ListingSnapshot,
    ListingWatch,
    Monitor,
    Observation,
    Run,
    Setting,
    Target,
    backup_omits,
    now,
)
from .engines import Gateway, ProviderError, apple_language, http_url
from .matching import match_app
from .regions import REGIONS, supported_country
from .service import Service, record
from .worker import Worker

Frequency = Literal["daily", "weekly", "biweekly", "monthly"]
Source = Literal[
    "apple_app_store", "google_play", "google_ai_mode", "google_ai_overview", "bing_copilot"
]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class KeyInput(Payload):
    api_key: SecretStr


class StorageCleanupInput(Payload):
    kind: Literal["responses", "images"]
    days: Literal[7, 30]
    confirm: Literal[True]


class ReplaceQueryInput(Payload):
    query: str = Field(min_length=1, max_length=500)


class OnboardingInput(Payload):
    action: Literal["skip", "dismiss", "restore"]
    step: Literal["app", "store", "ai"] | None = None

    @model_validator(mode="after")
    def require_step(self):
        if self.action == "skip" and self.step is None:
            raise ValueError("Choose a setup step to skip.")
        return self


class DiscoveryInput(Payload):
    platform: Literal["ios", "android"]
    query: str = Field(min_length=1, max_length=500)
    country: str = Field(default="us", pattern=r"^[a-z]{2}$")
    language: str = Field(default="en", pattern=r"^[a-z]{2,3}(?:-[a-z0-9]{2,3})?$")

    @model_validator(mode="after")
    def store_options(self):
        source = "apple_app_store" if self.platform == "ios" else "google_play"
        if not supported_country(source, self.country):
            raise ValueError("Choose a supported country for this store.")
        if self.platform == "ios":
            apple_language(self.language, self.country)
        return self


class AppInput(Payload):
    name: str = Field(min_length=1, max_length=160)
    candidate_tokens: list[str] = Field(min_length=1, max_length=2)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    website: str = Field(default="", max_length=500)

    @field_validator("website")
    @classmethod
    def valid_website(cls, value):
        if value and not http_url(value):
            raise ValueError("Use a full http:// or https:// website URL.")
        return value

    @field_validator("aliases")
    @classmethod
    def valid_aliases(cls, values):
        if any(not 2 <= len(value.strip()) <= 160 for value in values):
            raise ValueError("Aliases must contain 2 to 160 characters.")
        return list(dict.fromkeys(value.strip() for value in values))


class AppEdit(Payload):
    name: str = Field(min_length=1, max_length=160)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    website: str = ""
    archived: bool = False
    valid_website = field_validator("website")(AppInput.valid_website.__func__)
    valid_aliases = field_validator("aliases")(AppInput.valid_aliases.__func__)


class CompetitorInput(Payload):
    existing_app_id: int | None = Field(default=None, ge=1)
    app: AppInput | None = None
    monitor_ids: list[int] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def choose_app(self):
        if (self.existing_app_id is None) == (self.app is None):
            raise ValueError("Choose an existing app or connect a new competitor.")
        return self


class MonitorInput(Payload):
    query: str = Field(min_length=1, max_length=500)
    source: Source
    country: str = Field(default="us", pattern=r"^[a-z]{2}$")
    language: str = Field(default="en", pattern=r"^[a-z]{2,3}(?:-[a-z0-9]{2,3})?$")
    device: Literal["", "desktop", "mobile", "tablet"] = ""
    depth: int = Field(default=1, ge=1, le=4)
    frequency: Frequency = "daily"
    app_ids: list[int] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def source_options(self):
        if self.source == "apple_app_store":
            apple_language(self.language, self.country)
        if not supported_country(self.source, self.country):
            raise ValueError("Choose a supported country for this source.")
        if self.source == "google_play" and self.depth > 3:
            raise ValueError("Google Play supports at most three pages per check in AppTrail.")
        return self


class MonitorBatch(Payload):
    monitors: list[MonitorInput] = Field(min_length=1, max_length=2000)


class MonitorEdit(Payload):
    frequency: Frequency | None = None
    enabled: bool | None = None


class MonitorGroup(Payload):
    monitor_ids: list[int] = Field(min_length=1, max_length=2000)


class MonitorGroupEdit(MonitorGroup, MonitorEdit):
    pass


class SyncEdit(Payload):
    paused: bool


class TargetsInput(Payload):
    app_ids: list[int] = Field(min_length=1, max_length=100)


def create_app(directory=None, *, start_worker=True, gateway_factory=Gateway):
    config = Config(directory)
    lock = FileLock(config.directory / "instance.lock", thread_local=False)
    if start_worker:
        try:
            lock.acquire(timeout=0)
        except Timeout:
            raise RuntimeError("AppTrail is already running with this data directory.") from None
    db = None
    try:
        db = Database(config.directory)
        auth = Auth(db, config)
    except BaseException:
        if db is not None:
            db.close()
        lock.release()
        raise
    service = Service(db, config, gateway_factory)
    worker = Worker(service)
    restore_gate = RestoreGate()

    @asynccontextmanager
    async def lifespan(app):
        try:
            if start_worker:
                worker.start()
            yield
        finally:
            if start_worker:
                try:
                    worker.stop()
                finally:
                    lock.release()
            db.close()

    app = FastAPI(
        title="AppTrail",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.service, app.state.worker, app.state.auth = service, worker, auth

    @app.middleware("http")
    async def security(request: Request, call_next):
        response = await auth.guard(request, call_next)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' https: data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(RestoreMiddleware, gate=restore_gate)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        # Pydantic's default errors include submitted input, including API keys.
        return JSONResponse(
            {"detail": "; ".join(error["msg"] for error in exc.errors())}, status_code=422
        )

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(IntegrityError)
    async def duplicate(request, exc):
        return JSONResponse({"detail": "This app or query is already tracked."}, status_code=409)

    @app.get("/healthz")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/state")
    def state():
        return {**service.state(), "version": __version__}

    @app.post("/api/onboarding")
    def onboarding(payload: OnboardingInput):
        service.update_onboarding(payload.action, payload.step)
        return service.state()["onboarding"]

    @app.post("/api/hints/{hint}/dismiss")
    def dismiss_hint(hint: Literal["listing-history"]):
        with db.session.begin() as session:
            session.merge(Setting(key="dismissed_hints", value=[hint]))
        return {"ok": True}

    @app.post("/api/key")
    def save_key(payload: KeyInput):
        key = payload.api_key.get_secret_value().strip()
        if not key:
            raise ValueError("Enter a SerpApi API key.")
        if config.env_key:
            raise ValueError("The key is managed by the environment. Update it there and restart.")
        account = service.account(key)
        config.save_key(key)
        return {"account": account}

    @app.post("/api/account/refresh")
    def account():
        return service.account()

    @app.post("/api/discover")
    def discover(payload: DiscoveryInput):
        return {"candidates": service.discover(**payload.model_dump())}

    @app.post("/api/apps", status_code=201)
    def add_app(payload: AppInput):
        return {
            "id": service.save_app(
                payload.name, payload.candidate_tokens, payload.aliases, payload.website
            )
        }

    @app.post("/api/apps/{app_id}/listings", status_code=201)
    def add_listing(app_id: int, payload: AppInput):
        return {
            "id": service.save_app(
                payload.name, payload.candidate_tokens, payload.aliases, payload.website, app_id
            )
        }

    @app.post("/api/apps/{app_id}/competitors", status_code=201)
    def add_competitor(app_id: int, payload: CompetitorInput):
        if payload.app is not None:
            item = payload.app
            competitor_id = service.save_app(
                item.name,
                item.candidate_tokens,
                item.aliases,
                item.website,
                competitor_for=app_id,
                monitor_ids=payload.monitor_ids,
            )
        else:
            competitor_id = payload.existing_app_id
            service.add_competitor(app_id, competitor_id, payload.monitor_ids)
        return {"id": competitor_id, "monitor_ids": list(dict.fromkeys(payload.monitor_ids))}

    @app.delete("/api/apps/{app_id}/competitors/{competitor_id}")
    def remove_competitor(app_id: int, competitor_id: int):
        with db.session.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if not session.get(App, app_id) or not session.get(App, competitor_id):
                raise HTTPException(404, "App not found.")
            relation = session.get(Competitor, (app_id, competitor_id))
            if relation:
                session.delete(relation)
        return {"ok": True}

    @app.patch("/api/apps/{app_id}")
    def edit_app(app_id: int, payload: AppEdit):
        with db.session.begin() as session:
            item = session.get(App, app_id)
            if not item:
                raise HTTPException(404, "App not found.")
            for key, value in payload.model_dump(exclude_unset=True).items():
                setattr(item, key, value)
            if payload.archived:
                session.flush()
                monitor_ids = session.scalars(
                    select(Target.monitor_id).where(Target.app_id == app_id)
                ).all()
                for monitor_id in monitor_ids:
                    if not service.has_active_target(session, monitor_id):
                        service.cancel_runs(session, Run.monitor_id == monitor_id)
                listing_ids = select(Listing.id).where(Listing.app_id == app_id)
                service.cancel_runs(session, Run.listing_id.in_(listing_ids))
                watch_ids = select(ListingWatch.id).where(ListingWatch.listing_id.in_(listing_ids))
                service.cancel_runs(session, Run.watch_id.in_(watch_ids))
        return {"ok": True}

    @app.post("/api/apps/{app_id}/reanalyze")
    def reanalyze(app_id: int):
        with db.session.begin() as session:
            item = session.get(App, app_id)
            if not item:
                raise HTTPException(404, "App not found.")
            listings = session.scalars(select(Listing).where(Listing.app_id == app_id)).all()
            monitors = select(Target.monitor_id).where(Target.app_id == app_id)
            count = 0
            for run in session.scalars(
                select(Run).where(Run.monitor_id.in_(monitors), Run.status == "success")
            ):
                session.merge(
                    Observation(
                        run_id=run.id,
                        app_id=app_id,
                        data=match_app(item, listings, run.result),
                        retrospective=True,
                    )
                )
                count += 1
        return {"updated": count}

    @app.post("/api/apps/{app_id}/refresh")
    def refresh_profiles(app_id: int):
        with db.session.begin() as session:
            if not session.get(App, app_id):
                raise HTTPException(404, "App not found.")
            ids = []
            for listing in session.scalars(select(Listing).where(Listing.app_id == app_id)):
                existing = session.scalar(
                    select(Run).where(
                        Run.listing_id == listing.id, Run.status.in_(["queued", "running"])
                    )
                )
                if existing:
                    existing.params = {**existing.params, "manual": True}
                    ids.append(existing.id)
                    continue
                run = Run(
                    kind="profile",
                    listing_id=listing.id,
                    params={
                        **{
                            key: getattr(listing, key)
                            for key in ("platform", "external_id", "country", "language")
                        },
                        "manual": True,
                    },
                )
                session.add(run)
                session.flush()
                ids.append(run.id)
        return {"run_ids": ids}

    @app.get("/api/apps/{app_id}/profiles")
    def listing_profiles(app_id: int):
        with db.session() as session:
            if not session.get(App, app_id):
                raise HTTPException(404, "App not found.")
            return {"profiles": service.profile_history(session, app_id)}

    @app.post("/api/monitors", status_code=201)
    def add_monitor(payload: MonitorInput):
        data = payload.model_dump()
        frequency, app_ids = data.pop("frequency"), data.pop("app_ids")
        return service.add_monitor(data, frequency, app_ids)

    @app.get("/api/regions")
    def regions():
        return REGIONS

    @app.post("/api/monitors/batch", status_code=201)
    def add_monitors(payload: MonitorBatch):
        results = service.add_monitors([item.model_dump() for item in payload.monitors])
        return {"monitors": results, "created": sum(not item["reused"] for item in results)}

    @app.patch("/api/monitors/{monitor_id}")
    def edit_monitor(monitor_id: int, payload: MonitorEdit):
        with db.session.begin() as session:
            item = session.get(Monitor, monitor_id)
            if not item:
                raise HTTPException(404, "Query not found.")
            service.edit_monitors(session, [item], **payload.model_dump())
        return {"ok": True}

    @app.patch("/api/monitor-groups")
    def edit_monitor_group(payload: MonitorGroupEdit):
        with db.session.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            items = service.query_group(session, payload.monitor_ids)
            service.edit_monitors(
                session, items, frequency=payload.frequency, enabled=payload.enabled
            )
        return {"ok": True}

    @app.post("/api/monitor-groups/check")
    def check_monitor_group(payload: MonitorGroup):
        with db.session.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            items = service.query_group(session, payload.monitor_ids)
            ids = [service.enqueue(session, item, resume=True) for item in items]
        return {"run_ids": ids}

    @app.patch("/api/sync")
    def edit_sync(payload: SyncEdit):
        service.set_sync_paused(payload.paused)
        return {"paused": payload.paused}

    @app.post("/api/monitors/{monitor_id}/replace")
    def replace_monitor(monitor_id: int, payload: ReplaceQueryInput):
        return service.replace_monitor(monitor_id, payload.query)

    @app.post("/api/monitors/{monitor_id}/targets")
    def add_targets(monitor_id: int, payload: TargetsInput):
        with db.session.begin() as session:
            monitor = session.get(Monitor, monitor_id)
            if not monitor:
                raise HTTPException(404, "Query not found.")
            apps = service.target_apps(session, monitor.source, payload.app_ids)
            return service.attach_targets(session, monitor, apps)

    @app.get("/api/monitors/{monitor_id}/targets")
    def search_targets(monitor_id: int):
        with db.session() as session:
            monitor = session.get(Monitor, monitor_id)
            if not monitor:
                raise HTTPException(404, "Query not found.")
            run = session.scalar(
                select(Run)
                .where(Run.monitor_id == monitor_id, Run.status == "success")
                .order_by(Run.finished_at.desc(), Run.id.desc())
            )
            apps = session.scalars(
                select(App).join(Target).where(Target.monitor_id == monitor_id).order_by(App.name)
            ).all()
            targets = []
            for item in apps:
                observation = session.get(Observation, (run.id, item.id)) if run else None
                targets.append(
                    {
                        "app_id": item.id,
                        "data": observation.data if observation else None,
                        "retrospective": observation.retrospective if observation else False,
                    }
                )
            return {
                "monitor": record(monitor),
                "targets": targets,
                "run_id": run.id if run else None,
                "checked_at": (run.started_at or run.created_at) if run else None,
            }

    @app.delete("/api/monitors/{monitor_id}/targets/{app_id}")
    def detach(monitor_id: int, app_id: int):
        with db.session.begin() as session:
            target = session.get(Target, (monitor_id, app_id))
            if not target:
                raise HTTPException(404, "Tracking target not found.")
            session.delete(target)
            session.flush()
            if not service.has_active_target(session, monitor_id):
                service.cancel_runs(session, Run.monitor_id == monitor_id)
        return {"ok": True}

    @app.post("/api/monitors/{monitor_id}/check")
    def check(monitor_id: int):
        with db.session.begin() as session:
            item = session.get(Monitor, monitor_id)
            if not item:
                raise HTTPException(404, "Query not found.")
            return {"run_id": service.enqueue(session, item, resume=True)}

    @app.post("/api/check")
    def check_all(app_id: int | None = None, country: str | None = None, source: str | None = None):
        with db.session.begin() as session:
            active = select(Target.monitor_id).join(App).where(App.archived.is_(False))
            if app_id:
                active = active.where(Target.app_id == app_id)
            query = select(Monitor).where(Monitor.enabled.is_(True), Monitor.id.in_(active))
            if country:
                query = query.where(Monitor.country == country)
            if source:
                query = query.where(Monitor.source == source)
            ids = [service.enqueue(session, item, resume=True) for item in session.scalars(query)]
        return {"run_ids": ids}

    @app.get("/api/dashboard")
    def dashboard(
        app_id: int | None = None,
        days: int = Query(30, ge=1, le=3650),
        start: float | None = None,
        end: float | None = None,
        country: str | None = None,
        source: str | None = None,
    ):
        return service.dashboard(
            app_id, start if start is not None else now() - days * 86400, end, country, source
        )

    @app.get("/api/runs/{run_id}")
    def run_details(run_id: int):
        with db.session() as session:
            item = session.get(Run, run_id)
            if not item:
                raise HTTPException(404, "Run not found.")
            return {
                **record(item),
                "responses_omitted": backup_omits(session, "responses_through_run", item.id)
                and not item.responses,
                "responses_cleaned": not item.responses
                and item.status not in {"queued", "running"}
                and storage.removed_by_cleanup(
                    session, "responses", item.finished_at or item.started_at or item.created_at
                ),
                **(
                    {
                        "listing_snapshot_id": session.scalar(
                            select(ListingSnapshot.id).where(ListingSnapshot.run_id == item.id)
                        )
                    }
                    if item.kind == "listing_history"
                    else {}
                ),
                "observations": [
                    record(obs)
                    for obs in session.scalars(
                        select(Observation).where(Observation.run_id == run_id)
                    )
                ],
            }

    @app.get("/api/export.csv")
    def export(app_id: int | None = None, country: str | None = None, source: str | None = None):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as handle:
            path = Path(handle.name)
        try:
            with path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.writer(output)
                writer.writerow(
                    [
                        "checked_at",
                        "app_id",
                        "query",
                        "source",
                        "country",
                        "status",
                        "position",
                        "mentioned",
                        "cited",
                        "answer_available",
                        "app_name",
                        "monitor_id",
                        "language",
                        "device",
                        "depth",
                        "found",
                        "section",
                        "app_link",
                        "website_cited",
                        "retrospective",
                    ]
                )
                for row in service.export_observations(app_id, country, source):
                    values = [
                        datetime.fromtimestamp(row["checked_at"], UTC).isoformat(),
                        row["app_id"],
                        row["params"]["query"],
                        row["params"]["source"],
                        row["params"]["country"],
                        row["status"],
                        *[
                            row["data"].get(k, "")
                            for k in ("position", "mentioned", "cited", "answer_available")
                        ],
                        row["app_name"],
                        row["monitor_id"],
                        *[row["params"].get(k, "") for k in ("language", "device", "depth")],
                        *[
                            row["data"].get(k, "")
                            for k in ("found", "section", "app_link", "website_cited")
                        ],
                        row["retrospective"],
                    ]
                    writer.writerow(
                        [
                            "'" + value
                            if isinstance(value, str)
                            and value.startswith(("=", "+", "-", "@", "\t", "\r", "\n"))
                            else value
                            for value in values
                        ]
                    )
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return FileResponse(
            path,
            filename="apptrail-history.csv",
            media_type="text/csv",
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

    @app.get("/api/storage")
    def storage_usage():
        return storage.usage(db)

    @app.post("/api/storage/cleanup")
    def storage_cleanup(payload: StorageCleanupInput):
        with restore_gate.exclusive(operation="Storage cleanup"):
            running = worker.thread is not None and worker.thread.is_alive()
            if running:
                worker.stop()
            try:
                return storage.cleanup(db, payload.kind, payload.days)
            finally:
                if running:
                    worker.start()

    @app.get("/api/backup")
    def backup():
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as handle:
            path = Path(handle.name)
        try:
            db.backup(path, include_sessions=False, compact=True)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return FileResponse(
            path,
            filename="apptrail-backup.sqlite3",
            media_type="application/vnd.sqlite3",
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

    def replace_database(request, path):
        with restore_gate.exclusive():
            if request.url.path == "/api/auth/restore":
                auth.authorize_setup_restore(request, throttle=False)
            elif not auth.identify(request):
                raise HTTPException(401, "Sign in to AppTrail again before restoring.")
            running = worker.thread is not None and worker.thread.is_alive()
            if running:
                worker.stop()
            try:
                backups = config.directory / "backups"
                backups.mkdir(exist_ok=True, mode=0o700)
                with tempfile.NamedTemporaryFile(
                    dir=backups, prefix="before-restore-", suffix=".sqlite3", delete=False
                ) as handle:
                    recovery = Path(handle.name)
                try:
                    db.backup(recovery, include_sessions=False)
                except BaseException:
                    recovery.unlink(missing_ok=True)
                    raise
                db.restore(path)
                auth.setup_path.unlink(missing_ok=True)
            finally:
                if running:
                    worker.start()

    @app.post("/api/restore")
    @app.post("/api/auth/restore")
    async def restore(request: Request):
        if request.url.path == "/api/auth/restore":
            await run_in_threadpool(auth.authorize_setup_restore, request)
        if request.headers.get("x-apptrail-confirm-restore") != "overwrite":
            raise HTTPException(
                422, "Confirm that the backup will overwrite all current server data."
            )
        if int(request.headers.get("content-length", "0")) > MAX_RESTORE_BYTES:
            raise HTTPException(413, "SQLite backups must be 2 GB or smaller.")
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=config.directory) as directory:
            staging = Path(directory)
            upload = staging / "upload.sqlite3"
            size = 0
            with upload.open("wb") as handle:
                upload.chmod(0o600)
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_RESTORE_BYTES:
                        raise HTTPException(413, "SQLite backups must be 2 GB or smaller.")
                    handle.write(chunk)
            path = await run_in_threadpool(prepare_restore, upload, staging)
            await run_in_threadpool(replace_database, request, path)
        response = JSONResponse({"ok": True})
        response.delete_cookie(
            auth.cookie_name(request),
            path="/",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="strict",
        )
        return response

    static = Path(__file__).parent / "static"
    from .insights_api import register_insights

    register_insights(app, service)
    auth.routes(app, static)
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
