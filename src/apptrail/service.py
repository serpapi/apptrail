from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select, text

from .db import (
    App,
    Candidate,
    Competitor,
    Listing,
    ListingWatch,
    Monitor,
    Notification,
    Observation,
    Owner,
    Profile,
    Run,
    Setting,
    Target,
    now,
)
from .engines import SOURCES, Gateway
from .matching import match_app

FREQUENCIES = {"daily": 1, "weekly": 7, "biweekly": 14, "monthly": 30}


def next_check(timestamp, frequency):
    value = datetime.fromtimestamp(timestamp, UTC)
    delta = (
        relativedelta(months=1)
        if frequency == "monthly"
        else relativedelta(days=FREQUENCIES[frequency])
    )
    return (value + delta).timestamp()


def record(model, exclude=()):
    return {
        column.name: getattr(model, column.name)
        for column in model.__table__.columns
        if column.name not in exclude
    }


def observation_record(obs, run):
    return {
        "run_id": run.id,
        "app_id": obs.app_id,
        "monitor_id": run.monitor_id,
        "checked_at": run.started_at or run.created_at,
        "status": run.status,
        "params": run.params,
        "data": obs.data,
        "retrospective": obs.retrospective,
    }


def specification(monitor):
    return {
        name: getattr(monitor, name)
        for name in ("query", "source", "country", "language", "device", "depth")
    }


def signature(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


class Service:
    def __init__(self, db, config, gateway_factory=Gateway):
        self.db, self.config, self.gateway_factory = db, config, gateway_factory
        from .listing_history import ListingHistory

        self.listing_history = ListingHistory(self)

    def gateway(self):
        return self.gateway_factory(self.config)

    def account(self, key=None):
        value = self.gateway().account(key)
        with self.db.session.begin() as session:
            session.merge(Setting(key="account", value={"data": value, "checked_at": now()}))
        return value

    def discover(self, platform, query, country, language):
        results = self.gateway().discover(platform, query.strip(), country, language)
        with self.db.session.begin() as session:
            session.query(Candidate).filter(Candidate.expires_at < now()).delete()
            output = []
            for result in results:
                token = secrets.token_urlsafe(24)
                session.add(Candidate(token=token, data=result, expires_at=now() + 3600))
                output.append({**result, "token": token})
        return output

    def save_app(
        self, name, tokens, aliases, website, app_id=None, *, competitor_for=None, monitor_ids=()
    ):
        with self.db.session() as session:
            candidates = [session.get(Candidate, token) for token in tokens]
            if not candidates or any(
                item is None or item.expires_at < now() for item in candidates
            ):
                raise ValueError("Search for the app again; this selection has expired.")
            candidates = [item.data for item in candidates]
            if len({item["platform"] for item in candidates}) != len(candidates):
                raise ValueError("Choose only one listing per platform.")
            for item in candidates:
                exists = session.scalar(
                    select(Listing).where(
                        Listing.platform == item["platform"],
                        Listing.external_id == item["external_id"],
                    )
                )
                if exists:
                    raise ValueError("This listing is already tracked. Open its existing app.")
            if app_id and not session.get(App, app_id):
                raise ValueError("App not found.")
            if competitor_for is not None:
                self.competitor_monitors(
                    session, competitor_for, monitor_ids, {item["platform"] for item in candidates}
                )
        # Resolve chosen listings again through the product API before saving their identities.
        verified = []
        for item in candidates:
            result = self.gateway().product(
                item["platform"], item["external_id"], item["country"], item["language"]
            )
            verified.append({**item, **{k: v for k, v in result.items() if v not in ("", None)}})
        with self.db.session.begin() as session:
            app = (
                session.get(App, app_id)
                if app_id
                else App(name=name, aliases=aliases, website=website)
            )
            if not app_id:
                session.add(app)
                session.flush()
            for item in verified:
                existing = session.scalar(
                    select(Listing).where(
                        Listing.app_id == app.id, Listing.platform == item["platform"]
                    )
                )
                if existing:
                    raise ValueError("This app already has a listing for that platform.")
                listing = Listing(app_id=app.id, **item)
                session.add(listing)
                session.flush()
                session.add(Profile(listing_id=listing.id, data=listing.metadata_json))
            if competitor_for is not None:
                self.link_competitor(session, competitor_for, app.id, monitor_ids)
            for token in tokens:
                session.delete(session.get(Candidate, token))
            return app.id

    def competitor_monitors(self, session, app_id, monitor_ids, platforms):
        original = session.get(App, app_id)
        if original is None or original.archived:
            raise ValueError("Choose an active app to compare with.")
        monitors = []
        for monitor_id in dict.fromkeys(monitor_ids):
            monitor = session.get(Monitor, monitor_id)
            if monitor is None or not session.get(Target, (monitor_id, app_id)):
                raise ValueError("Choose searches that already track the original app.")
            platform = SOURCES[monitor.source].get("platform")
            if platform and platform not in platforms:
                store = "App Store" if platform == "ios" else "Google Play"
                raise ValueError(f"Connect a {store} listing or deselect its searches.")
            monitors.append(monitor)
        return monitors

    def link_competitor(self, session, app_id, competitor_id, monitor_ids):
        if app_id == competitor_id:
            raise ValueError("Choose a different app as the competitor.")
        rival = session.get(App, competitor_id)
        if rival is None or rival.archived:
            raise ValueError("Choose an active competitor app.")
        platforms = set(
            session.scalars(select(Listing.platform).where(Listing.app_id == competitor_id))
        )
        if not platforms:
            raise ValueError("Connect at least one store listing for the competitor.")
        monitors = self.competitor_monitors(session, app_id, monitor_ids, platforms)
        if not session.get(Competitor, (app_id, competitor_id)):
            session.add(Competitor(app_id=app_id, competitor_id=competitor_id))
        for monitor in monitors:
            self.attach_targets(session, monitor, [rival])

    def add_competitor(self, app_id, competitor_id, monitor_ids):
        with self.db.session.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            self.link_competitor(session, app_id, competitor_id, monitor_ids)

    def add_monitor(self, spec, frequency, app_ids):
        return self.add_monitors([{**spec, "frequency": frequency, "app_ids": app_ids}])[0]

    def add_monitors(self, entries):
        results = []
        with self.db.session.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            for entry in entries:
                spec = dict(entry)
                frequency, app_ids = spec.pop("frequency"), spec.pop("app_ids")
                if spec["source"] == "bing_copilot":
                    spec.update(country="global", language="auto", device="", depth=1)
                elif SOURCES[spec["source"]]["kind"] == "ai":
                    spec["depth"] = 1
                if spec["source"] == "google_play":
                    spec["device"] = ""
                spec["query"] = spec["query"].strip()
                apps = self.target_apps(session, spec["source"], app_ids)
                monitor = session.scalar(
                    select(Monitor).where(Monitor.signature == signature(spec))
                )
                reused = monitor is not None
                if monitor is None:
                    monitor = Monitor(**spec, signature=signature(spec), frequency=frequency)
                    session.add(monitor)
                    session.flush()
                self.attach_targets(session, monitor, apps)
                session.flush()
                if not reused:
                    self.enqueue(session, monitor)
                results.append(
                    {
                        "id": monitor.id,
                        "reused": reused,
                        "frequency": monitor.frequency,
                        "enabled": monitor.enabled,
                    }
                )
        return results

    def target_apps(self, session, source, app_ids):
        apps = [session.get(App, app_id) for app_id in dict.fromkeys(app_ids)]
        if not apps or any(app is None or app.archived for app in apps):
            raise ValueError("Choose at least one active app.")
        platform = SOURCES[source].get("platform")
        if platform:
            for app in apps:
                if not session.scalar(
                    select(Listing.id).where(Listing.app_id == app.id, Listing.platform == platform)
                ):
                    store = "App Store" if platform == "ios" else "Google Play"
                    raise ValueError(
                        f"{app.name} needs a verified {store} listing for this search."
                    )
        return apps

    def attach_targets(self, session, monitor, apps):
        added = []
        runs = session.scalars(
            select(Run).where(Run.monitor_id == monitor.id, Run.status == "success")
        ).all()
        for app in apps:
            if session.get(Target, (monitor.id, app.id)):
                continue
            session.add(Target(monitor_id=monitor.id, app_id=app.id))
            added.append(app.id)
            listings = session.scalars(select(Listing).where(Listing.app_id == app.id)).all()
            for run in runs:
                session.merge(
                    Observation(
                        run_id=run.id,
                        app_id=app.id,
                        data=match_app(app, listings, run.result),
                        retrospective=True,
                    )
                )
        return {"added_app_ids": added, "saved_checks": len(runs) if added else 0}

    def enqueue(self, session, monitor, *, resume=False):
        existing = session.scalar(
            select(Run).where(Run.monitor_id == monitor.id, Run.status.in_(["queued", "running"]))
        )
        if existing:
            if resume and existing.params.get("cancel_requested"):
                existing.params = {
                    k: v for k, v in existing.params.items() if k != "cancel_requested"
                }
            return existing.id
        run = Run(monitor_id=monitor.id, params=specification(monitor))
        session.add(run)
        session.flush()
        return run.id

    def has_active_target(self, session, monitor_id):
        return (
            session.scalar(
                select(Target.app_id)
                .join(App)
                .where(Target.monitor_id == monitor_id, App.archived.is_(False))
                .limit(1)
            )
            is not None
        )

    def cancel_runs(self, session, *criteria):
        for run in session.scalars(
            select(Run).where(*criteria, Run.status.in_(["queued", "running"]))
        ):
            if run.status == "queued":
                run.status, run.finished_at = "cancelled", now()
            else:
                # An in-flight request can finish, but must not spend credits on a retry.
                run.params = {**run.params, "cancel_requested": True}

    def sync_paused(self, session=None):
        if session is None:
            with self.db.session() as session:
                return self.sync_paused(session)
        setting = session.get(Setting, "sync_paused")
        return bool(setting and setting.value)

    def set_sync_paused(self, paused):
        with self.db.session.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            session.merge(Setting(key="sync_paused", value=paused))
        if not paused:
            self.dispatch()

    def query_group(self, session, monitor_ids):
        items = session.scalars(select(Monitor).where(Monitor.id.in_(monitor_ids))).all()
        if len(items) != len(set(monitor_ids)):
            raise ValueError("A query was removed. Reload this page and try again.")
        definitions = {
            (item.query.strip(), item.source in {"apple_app_store", "google_play"})
            for item in items
        }
        if len(definitions) != 1:
            raise ValueError("Choose variants of a single store query or AI question.")
        return items

    def edit_monitors(self, session, items, *, frequency=None, enabled=None):
        for item in items:
            if frequency:
                item.frequency = frequency
                latest = session.scalar(
                    select(Run)
                    .where(Run.monitor_id == item.id, Run.status == "success")
                    .order_by(Run.finished_at.desc())
                )
                item.next_run_at = next_check(latest.finished_at, frequency) if latest else now()
            if enabled is not None:
                item.enabled = enabled
                if not enabled:
                    self.cancel_runs(session, Run.monitor_id == item.id)
                else:
                    item.next_run_at = now()
                    self.enqueue(session, item, resume=True)

    def replace_monitor(self, monitor_id, query):
        with self.db.session.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            old = session.get(Monitor, monitor_id)
            if not old:
                raise ValueError("Query not found.")
            spec = {**specification(old), "query": query.strip()}
            if signature(spec) == old.signature:
                raise ValueError("Enter a different query to start a new series.")
            apps = session.scalars(
                select(App).join(Target).where(Target.monitor_id == old.id, App.archived.is_(False))
            ).all()
            if not apps:
                raise ValueError("Choose at least one active app before replacing this query.")
            replacement = session.scalar(
                select(Monitor).where(Monitor.signature == signature(spec))
            )
            reused = replacement is not None
            if replacement is None:
                replacement = Monitor(**spec, signature=signature(spec), frequency=old.frequency)
                session.add(replacement)
                session.flush()
            self.attach_targets(session, replacement, apps)
            replacement.enabled, replacement.next_run_at = True, now()
            self.enqueue(session, replacement, resume=True)
            old.enabled = False
            self.cancel_runs(session, Run.monitor_id == old.id)
            return {"id": replacement.id, "reused": reused, "frequency": replacement.frequency}

    def dispatch(self):
        if not self.config.api_key:
            return
        with self.db.session.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if not session.get(Owner, 1) or self.sync_paused(session):
                return
            active = (
                select(Target.monitor_id)
                .join(App, App.id == Target.app_id)
                .where(App.archived.is_(False))
            )
            for monitor in session.scalars(
                select(Monitor).where(
                    Monitor.enabled.is_(True), Monitor.next_run_at <= now(), Monitor.id.in_(active)
                )
            ):
                self.enqueue(session, monitor)
            self.listing_history.dispatch(session)

    def update_onboarding(self, action, step=None):
        with self.db.session.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            saved = session.get(Setting, "onboarding")
            value = dict(saved.value) if saved else {"skipped": [], "dismissed": False}
            if action == "restore":
                value = {"skipped": [], "dismissed": False}
            elif action == "dismiss":
                value["dismissed"] = True
            elif action == "skip":
                value["skipped"] = sorted(set(value.get("skipped", [])) | {step})
            session.merge(Setting(key="onboarding", value=value))

    def state(self):
        with self.db.session() as session:
            competitor_parents = {}
            for relation in session.scalars(select(Competitor)):
                competitor_parents.setdefault(relation.competitor_id, []).append(relation.app_id)
            apps = []
            for app in session.scalars(select(App).order_by(App.created_at)):
                apps.append(
                    {
                        **record(app),
                        "competitor_for": competitor_parents.get(app.id, []),
                        "listings": [
                            record(item)
                            for item in session.scalars(
                                select(Listing).where(Listing.app_id == app.id)
                            )
                        ],
                    }
                )
            monitors = []
            ranked_runs = select(
                Run.id,
                Run.monitor_id,
                Run.status,
                func.row_number()
                .over(partition_by=Run.monitor_id, order_by=(Run.created_at.desc(), Run.id.desc()))
                .label("row_number"),
            ).subquery()
            latest_runs = {
                row.monitor_id: row
                for row in session.execute(select(ranked_runs).where(ranked_runs.c.row_number == 1))
            }
            targets_by_monitor = {}
            for target in session.scalars(select(Target)):
                targets_by_monitor.setdefault(target.monitor_id, []).append(target.app_id)
            for monitor in session.scalars(select(Monitor).order_by(Monitor.created_at.desc())):
                latest = latest_runs.get(monitor.id)
                targets = targets_by_monitor.get(monitor.id, [])
                monitors.append(
                    {
                        **record(monitor),
                        "app_ids": targets,
                        "latest_status": latest.status if latest else None,
                        "latest_run_id": latest.id if latest else None,
                    }
                )
            account = session.get(Setting, "account")
            dismissed_hints = session.get(Setting, "dismissed_hints")
            active_ids = {app["id"] for app in apps if not app["archived"]}
            saved_onboarding = session.get(Setting, "onboarding")
            onboarding = {
                "skipped": [],
                "dismissed": False,
                **(saved_onboarding.value if saved_onboarding else {}),
            }
            completed = []
            if any(app["listings"] for app in apps if app["id"] in active_ids):
                completed.append("app")
            tracked_sources = {
                monitor["source"]
                for monitor in monitors
                if active_ids.intersection(monitor["app_ids"])
            }
            if tracked_sources.intersection({"apple_app_store", "google_play"}):
                completed.append("store")
            if tracked_sources.difference({"apple_app_store", "google_play"}):
                completed.append("ai")
            onboarding["completed"] = completed
            onboarding["visible"] = not onboarding["dismissed"] and bool(
                {"app", "store", "ai"}.difference(completed, onboarding["skipped"])
            )
            estimate_low = estimate_high = 0
            for monitor in monitors:
                if not monitor["enabled"] or not active_ids.intersection(monitor["app_ids"]):
                    continue
                checks = 30 / FREQUENCIES[monitor["frequency"]]
                calls = monitor["depth"] if monitor["source"] == "google_play" else 1
                estimate_low += checks * calls
                estimate_high += checks * (calls + (monitor["source"] == "google_ai_overview"))
            current_job = (
                session.execute(
                    select(Run.id, Run.status, Run.kind, Run.params, Run.available_at)
                    .where(Run.status.in_(["queued", "running"]))
                    .order_by((Run.status == "running").desc(), Run.available_at, Run.id)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            listing_credits = sum(
                30 / FREQUENCIES[watch.frequency]
                for watch in session.scalars(
                    select(ListingWatch)
                    .join(Listing)
                    .join(App)
                    .where(ListingWatch.enabled.is_(True), App.archived.is_(False))
                )
            )
            return {
                "configured": bool(self.config.api_key),
                "sync_paused": self.sync_paused(session),
                "key_from_environment": bool(self.config.env_key),
                "apps": apps,
                "monitors": monitors,
                "latest_observations": self.latest_observations(session),
                "onboarding": onboarding,
                "dismissed_hints": dismissed_hints.value if dismissed_hints else [],
                "listing_history_started": bool(session.scalar(select(ListingWatch.id).limit(1))),
                "sources": SOURCES,
                "account": account.value if account else None,
                "estimated_monthly": {
                    "min": round(estimate_low + listing_credits),
                    "max": round(estimate_high + listing_credits),
                },
                "listing_monthly_credits": round(listing_credits, 1),
                "notification_unread": session.scalar(
                    select(func.count())
                    .select_from(Notification)
                    .where(Notification.read.is_(False), Notification.dismissed.is_(False))
                ),
                "jobs": dict(
                    session.execute(select(Run.status, func.count()).group_by(Run.status)).all()
                ),
                "current_job": dict(current_job) if current_job else None,
                "data_directory": str(self.config.directory),
            }

    def latest_observations(self, session):
        ranked = (
            select(
                Observation.run_id,
                Observation.app_id,
                func.row_number()
                .over(
                    partition_by=(Run.monitor_id, Observation.app_id),
                    order_by=(func.coalesce(Run.started_at, Run.created_at).desc(), Run.id.desc()),
                )
                .label("row_number"),
            )
            .join(Run)
            .subquery()
        )
        query = (
            select(Observation, Run)
            .join(Run)
            .join(
                ranked,
                (ranked.c.run_id == Observation.run_id) & (ranked.c.app_id == Observation.app_id),
            )
            .where(ranked.c.row_number == 1)
        )
        return [observation_record(obs, run) for obs, run in session.execute(query)]

    def profile_history(self, session, app_id=None, start=0, end=None, country=None, source=None):
        end = end or now()
        profile_query = (
            select(Profile, Listing)
            .join(Listing)
            .where(Profile.checked_at >= start, Profile.checked_at <= end)
            .order_by(Profile.checked_at)
        )
        if app_id:
            profile_query = profile_query.where(Listing.app_id == app_id)
        if country:
            profile_query = profile_query.where(Listing.country == country)
        if source:
            profile_query = profile_query.where(
                Listing.platform == SOURCES.get(source, {}).get("platform", "")
            )
        profiles = [
            {
                **record(profile),
                "app_id": listing.app_id,
                "platform": listing.platform,
                "country": listing.country,
            }
            for profile, listing in session.execute(profile_query)
        ]
        return profiles

    def observation_query(
        self, app_id=None, start=0, end=None, country=None, source=None, *, include_archived=True
    ):
        checked_at = func.coalesce(Run.started_at, Run.created_at)
        query = (
            select(Observation, Run)
            .join(Run)
            .join(App, App.id == Observation.app_id)
            .where(Run.kind == "search", checked_at >= start, checked_at <= (end or now()))
            .order_by(checked_at, Run.id)
        )
        if app_id:
            query = query.where(Observation.app_id == app_id)
        if not include_archived:
            query = query.where(App.archived.is_(False))
        if country:
            query = query.where(Run.params["country"].as_string() == country)
        if source:
            query = query.where(Run.params["source"].as_string() == source)
        return query

    def export_observations(self, app_id=None, country=None, source=None):
        with self.db.session() as session:
            query = self.observation_query(app_id, country=country, source=source).add_columns(
                App.name
            )
            for obs, run, app_name in session.execute(query.execution_options(yield_per=250)):
                yield {**observation_record(obs, run), "app_name": app_name}

    def dashboard(self, app_id=None, start=0, end=None, country=None, source=None):
        end = end or now()
        checked_at = func.coalesce(Run.started_at, Run.created_at)
        with self.db.session() as session:
            query = self.observation_query(
                app_id, start, end, country, source, include_archived=bool(app_id)
            )
            rows = [observation_record(obs, run) for obs, run in session.execute(query)]
            profiles = self.profile_history(session, app_id, start, end, country, source)
            recent = (
                select(Run)
                .where(checked_at >= start, checked_at <= end)
                .order_by(checked_at.desc(), Run.id.desc())
                .limit(40)
            )
            if app_id:
                recent = (
                    select(Run)
                    .where(
                        (
                            Run.monitor_id.in_(
                                select(Target.monitor_id).where(Target.app_id == app_id)
                            )
                            | Run.listing_id.in_(select(Listing.id).where(Listing.app_id == app_id))
                            | Run.watch_id.in_(
                                select(ListingWatch.id)
                                .join(Listing)
                                .where(Listing.app_id == app_id)
                            )
                        ),
                        checked_at >= start,
                        checked_at <= end,
                    )
                    .order_by(checked_at.desc(), Run.id.desc())
                    .limit(40)
                )
            if country:
                recent = recent.where(Run.params["country"].as_string() == country)
            if source:
                recent = recent.where(Run.params["source"].as_string() == source)
            runs = [record(run, exclude={"responses", "result"}) for run in session.scalars(recent)]
            return {"observations": rows, "profiles": profiles, "runs": runs}
