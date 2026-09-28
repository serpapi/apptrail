import math
from typing import Literal

from fastapi import HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from .db import App, Listing, ListingAsset, ListingWatch, Notification, Run, now
from .insights import query_matrix, ranking_distribution
from .regions import supported_country
from .service import next_check, record


class WatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    listing_id: int
    country: str = Field(pattern=r"^[a-z]{2}$")
    language: str = Field(default="en", pattern=r"^(?:auto|[a-z]{2,3}(?:-[a-z0-9]{2,3})?)$")
    frequency: Literal["daily", "weekly", "biweekly", "monthly"] = "weekly"


class WatchEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    frequency: Literal["daily", "weekly", "biweekly", "monthly"] | None = None


class NotificationEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["read", "dismiss"]


def set_watch_frequency(session, watch, frequency):
    if watch.frequency == frequency:
        return
    watch.frequency = frequency
    latest = session.scalar(
        select(Run.finished_at)
        .where(Run.watch_id == watch.id, Run.status == "success")
        .order_by(Run.finished_at.desc())
        .limit(1)
    )
    watch.next_run_at = next_check(latest, frequency) if latest is not None else now()


def register_insights(app, service):
    db, history = service.db, service.listing_history

    @app.get("/api/notifications")
    def notifications():
        with db.session() as session:
            return {
                "notifications": [
                    record(item)
                    for item in session.scalars(
                        select(Notification)
                        .where(Notification.dismissed.is_(False))
                        .order_by(Notification.id.desc())
                        .limit(100)
                    )
                ]
            }

    @app.post("/api/notifications/read")
    def read_notifications():
        with db.session.begin() as session:
            session.execute(update(Notification).values(read=True))
        return {"ok": True}

    @app.patch("/api/notifications/{notification_id}")
    def edit_notification(notification_id: int, payload: NotificationEdit):
        with db.session.begin() as session:
            item = session.get(Notification, notification_id)
            if not item:
                raise HTTPException(404, "Notification not found.")
            item.read = True
            if payload.action == "dismiss":
                item.dismissed = True
        return {"ok": True}

    @app.get("/api/monitors/{monitor_id}/matrix")
    def matrix(monitor_id: int):
        return query_matrix(service, monitor_id)

    @app.get("/api/ranking-distribution")
    def distribution(
        app_id: int | None = None,
        country: str | None = None,
        source: str | None = None,
        days: int = Query(30, ge=1, le=3650),
        start: float | None = None,
        end: float | None = None,
    ):
        end = end if end is not None else now()
        start = start if start is not None else end - days * 86400
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or not 0 < end - start <= 3650 * 86400
        ):
            raise ValueError("Choose a valid date range of up to ten years.")
        from .service import observation_record

        with db.session() as session:
            rows = [
                observation_record(obs, run)
                for obs, run in session.execute(
                    service.observation_query(
                        app_id,
                        start - (end - start),
                        end,
                        country,
                        source,
                        include_archived=bool(app_id),
                    )
                )
            ]
        return ranking_distribution(rows, start, end)

    @app.get("/api/listing-watches")
    def watches():
        return {"watches": history.watches()}

    @app.post("/api/listing-watches", status_code=201)
    def enable_watch(payload: WatchInput):
        if not service.config.api_key:
            raise ValueError("Connect SerpApi in Settings before enabling listing history.")
        with db.session.begin() as session:
            listing = session.get(Listing, payload.listing_id)
            if not listing or session.get(App, listing.app_id).archived:
                raise ValueError("Choose a listing from an active app.")
            source = "apple_app_store" if listing.platform == "ios" else "google_play"
            if not supported_country(source, payload.country):
                raise ValueError("Choose a supported country for this store.")
            if listing.platform == "android" and payload.language == "auto":
                raise ValueError("Choose a Google Play language code, such as en or pt-br.")
            language = "auto" if listing.platform == "ios" else payload.language
            watch = session.scalar(
                select(ListingWatch).where(
                    ListingWatch.listing_id == listing.id,
                    ListingWatch.country == payload.country,
                    ListingWatch.language == language,
                )
            )
            if watch is None:
                watch = ListingWatch(
                    listing_id=listing.id, country=payload.country, language=language
                )
                session.add(watch)
                session.flush()
            was_enabled = watch.enabled
            set_watch_frequency(session, watch, payload.frequency)
            watch.enabled = True
            if not was_enabled:
                watch.next_run_at = now()
                history.enqueue(session, watch, resume=True)
            return {"id": watch.id}

    @app.patch("/api/listing-watches/{watch_id}")
    def edit_watch(watch_id: int, payload: WatchEdit):
        with db.session.begin() as session:
            watch = session.get(ListingWatch, watch_id)
            if not watch:
                raise HTTPException(404, "Listing tracking not found.")
            if payload.frequency:
                set_watch_frequency(session, watch, payload.frequency)
            if payload.enabled is not None:
                was_enabled = watch.enabled
                if payload.enabled:
                    listing = session.get(Listing, watch.listing_id)
                    if session.get(App, listing.app_id).archived:
                        raise ValueError("Unarchive the app before resuming listing history.")
                    if not service.config.api_key:
                        raise ValueError("Connect SerpApi before resuming listing history.")
                watch.enabled = payload.enabled
                if not watch.enabled:
                    service.cancel_runs(session, Run.watch_id == watch.id)
                elif not was_enabled:
                    watch.next_run_at = now()
                    history.enqueue(session, watch, resume=True)
        return {"ok": True}

    @app.post("/api/listing-watches/{watch_id}/check")
    def check_watch(watch_id: int):
        with db.session.begin() as session:
            watch = session.get(ListingWatch, watch_id)
            if not watch or not watch.enabled:
                raise ValueError("Enable this listing history before checking it.")
            listing = session.get(Listing, watch.listing_id)
            if session.get(App, listing.app_id).archived:
                raise ValueError("Unarchive the app before checking it.")
            if not service.config.api_key:
                raise ValueError("Connect SerpApi before checking listing history.")
            run = history.enqueue(session, watch, resume=True)
            session.flush()
            return {"run_id": run.id}

    @app.get("/api/listing-watches/{watch_id}/history")
    def timeline(watch_id: int, include_unchanged: bool = False, before_id: int | None = None):
        return history.timeline(watch_id, include_unchanged, before_id)

    @app.get("/api/listing-snapshots/{snapshot_id}")
    def comparison(snapshot_id: int):
        return history.comparison(snapshot_id)

    @app.get("/api/listing-assets/{asset_id}")
    def asset(asset_id: str):
        if len(asset_id) != 64 or any(c not in "0123456789abcdef" for c in asset_id):
            raise HTTPException(404, "Image not found.")
        with db.session() as session:
            item = session.get(ListingAsset, asset_id)
            if not item:
                raise HTTPException(404, "Image not found.")
            return Response(item.content, media_type=item.mime)
