from __future__ import annotations

import logging
import threading

from sqlalchemy import select, text, update

from .db import App, Listing, ListingWatch, Monitor, Observation, Owner, Profile, Run, Target, now
from .engines import ProviderError
from .insights import update_rank_alert
from .listing_history import archive_media, snapshot_content
from .matching import match_app
from .service import next_check

logger = logging.getLogger("apptrail.worker")


class Worker:
    def __init__(self, service):
        self.service, self.db = service, service.db
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.stop_event.clear()
        with self.db.session.begin() as session:
            for run in session.scalars(select(Run).where(Run.status.in_(["queued", "running"]))):
                if reason := self.cancellation_reason(session, run):
                    run.status, run.finished_at = "cancelled", now()
                    run.error = reason
            session.flush()
            session.execute(
                update(Run)
                .where(Run.status == "running")
                .values(
                    status="queued",
                    available_at=now(),
                    error="Interrupted; resuming after restart.",
                )
            )
        self.thread = threading.Thread(target=self.loop, name="apptrail-worker", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            # Keep the workspace lock until the current request has finished saving.
            self.thread.join()

    def loop(self):
        last_dispatch = last_account = 0
        while not self.stop_event.is_set():
            try:
                if now() - last_dispatch >= 60:
                    self.service.dispatch()
                    last_dispatch = now()
                processed = self.process_one()
                if (
                    processed
                    and not self.stop_event.is_set()
                    and not self.service.sync_paused()
                    and now() - last_account > 300
                ):
                    try:
                        self.service.account()
                    except ProviderError:
                        pass
                    last_account = now()
            except Exception:
                # Never log provider exceptions that could contain a credential-bearing URL.
                logger.error(
                    "Background work failed; check run history and restart if this persists."
                )
            self.stop_event.wait(1)

    def process_one(self):
        with self.db.session.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if not session.get(Owner, 1) or self.service.sync_paused(session):
                return False
            run = session.scalar(
                select(Run)
                .where(Run.status == "queued", Run.available_at <= now())
                .order_by(Run.created_at)
            )
            if not run:
                return False
            if reason := self.cancellation_reason(session, run):
                run.status, run.finished_at = "cancelled", now()
                run.error = reason
                return True
            claimed = session.execute(
                update(Run)
                .where(Run.id == run.id, Run.status == "queued")
                .values(status="running", started_at=now(), attempts=Run.attempts + 1)
            )
            if not claimed.rowcount:
                return False
            run_id = run.id
        gateway = self.service.gateway()
        try:
            with self.db.session() as session:
                run = session.get(Run, run_id)
                params, kind = run.params, run.kind
            assets = []
            if kind in {"profile", "listing_history"}:
                product = gateway.product(
                    params["platform"], params["external_id"], params["country"], params["language"]
                )
                result = {"kind": "profile", "profile": product}
                if kind == "listing_history":
                    raw = gateway.responses[-1].get("data", {}) if gateway.responses else {}
                    content, assets = archive_media(snapshot_content(product, raw))
                    result = {"kind": "listing_history", "snapshot": content}
            else:
                result = gateway.search(params).result
            with self.db.session.begin() as session:
                run = session.get(Run, run_id)
                run.status, run.finished_at, run.result, run.error = "success", now(), result, ""
                run.responses = run.responses + gateway.responses
                run.requests_count += gateway.requests_count
                if reason := self.cancellation_reason(session, run):
                    run.status, run.error = "cancelled", reason
                    return True
                if kind == "listing_history":
                    self.service.listing_history.save(session, run, content, assets)
                elif kind == "profile":
                    listing = session.get(Listing, run.listing_id)
                    if listing:
                        listing.metadata_json = product["metadata_json"]
                        listing.title = product["title"]
                        if product["icon"]:
                            listing.icon = product["icon"]
                        session.add(Profile(listing_id=listing.id, data=listing.metadata_json))
                else:
                    for app in session.scalars(
                        select(App)
                        .join(Target)
                        .where(Target.monitor_id == run.monitor_id, App.archived.is_(False))
                    ):
                        listings = session.scalars(
                            select(Listing).where(Listing.app_id == app.id)
                        ).all()
                        data = match_app(app, listings, result)
                        session.merge(Observation(run_id=run.id, app_id=app.id, data=data))
                        update_rank_alert(session, run, app.id, data)
                self.schedule_next(session, run)
        except Exception as exc:
            # Unexpected internal errors are represented without leaking arbitrary exception text.
            error = (
                str(exc)
                if isinstance(exc, ProviderError)
                else "This check could not be completed. Please try again."
            )
            with self.db.session.begin() as session:
                run = session.get(Run, run_id)
                run.error = error
                run.responses = run.responses + gateway.responses
                run.requests_count += gateway.requests_count
                if self.cancellation_reason(session, run):
                    run.status, run.finished_at = "cancelled", now()
                elif isinstance(exc, ProviderError) and exc.retryable and run.attempts < 3:
                    run.status, run.available_at = "queued", now() + 30 * (2 ** (run.attempts - 1))
                else:
                    run.status, run.finished_at = "error", now()
                    if run.monitor_id:
                        for app_id in session.scalars(
                            select(Target.app_id)
                            .join(App)
                            .where(Target.monitor_id == run.monitor_id, App.archived.is_(False))
                        ):
                            session.merge(
                                Observation(run_id=run.id, app_id=app_id, data={"error": error})
                            )
                            update_rank_alert(session, run, app_id, {})
                    self.schedule_next(session, run)
        return True

    def cancellation_reason(self, session, run):
        if run.params.get("cancel_requested"):
            return "This check was cancelled."
        if run.kind == "listing_history":
            watch = session.get(ListingWatch, run.watch_id) if run.watch_id else None
            if not watch or not watch.enabled:
                return "Listing history is paused."
            listing = session.get(Listing, watch.listing_id)
            app = session.get(App, listing.app_id) if listing else None
            if not app or app.archived:
                return "This app is no longer active."
        elif run.kind == "profile":
            if not run.params.get("manual"):
                return "Automatic listing refreshes are disabled."
            listing = session.get(Listing, run.listing_id) if run.listing_id else None
            app = session.get(App, listing.app_id) if listing else None
            if not app or app.archived:
                return "This app is no longer active."
        elif not self.service.has_active_target(session, run.monitor_id):
            return "No active apps are tracking this search."
        return None

    def schedule_next(self, session, run):
        if run.watch_id and (watch := session.get(ListingWatch, run.watch_id)):
            watch.next_run_at = next_check(run.finished_at, watch.frequency)
        if run.monitor_id and (monitor := session.get(Monitor, run.monitor_id)):
            monitor.next_run_at = next_check(run.finished_at, monitor.frequency)
