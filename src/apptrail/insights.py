"""Analysis of saved checks; never makes provider requests."""

from sqlalchemy import select

from .db import App, Monitor, Notification, Observation, RankAlertState, Run, Target

STORE_SOURCES = {"apple_app_store", "google_play"}
BUCKETS = ("top3", "top10", "top50", "beyond50", "not_found")


def rank_bucket(data):
    position = data.get("position")
    if isinstance(position, (int, float)) and position > 0:
        return (
            "top3"
            if position <= 3
            else "top10"
            if position <= 10
            else "top50"
            if position <= 50
            else "beyond50"
        )
    return "featured" if data.get("found") else "not_found"


def ranking_distribution(rows, start, end):
    """Compare latest observations, once per app/monitor, across equal windows."""
    previous_start = start - (end - start)
    current, previous = {}, {}
    for row in sorted(rows, key=lambda r: (r["checked_at"], r["run_id"])):
        if row["params"].get("source") not in STORE_SOURCES:
            continue
        time = row["checked_at"]
        group = (
            current
            if start <= time <= end
            else previous
            if previous_start <= time < start
            else None
        )
        if group is not None:
            group[(row["monitor_id"], row["app_id"])] = row
    counts = dict.fromkeys(BUCKETS, 0)
    deltas = dict.fromkeys(BUCKETS, 0)
    entries, movements, paired = [], [], 0
    failed = featured = 0
    for key, row in current.items():
        if row["status"] != "success":
            failed += 1
            continue
        bucket = rank_bucket(row["data"])
        if bucket == "featured":
            featured += 1
            continue
        counts[bucket] += 1
        entries.append({**row, "bucket": bucket})
        before = previous.get(key)
        if not before or before["status"] != "success":
            continue
        old_bucket = rank_bucket(before["data"])
        if old_bucket not in BUCKETS:
            continue
        paired += 1
        deltas[old_bucket] -= 1
        deltas[bucket] += 1
        old, new = before["data"].get("position"), row["data"].get("position")
        if old != new:
            movements.append(
                {**row, "before": old, "after": new, "delta": old - new if old and new else None}
            )
    movements.sort(key=lambda r: (r["delta"] is None, abs(r["delta"] or 0)), reverse=True)
    return {
        "counts": counts,
        "deltas": deltas,
        "entries": entries,
        "movements": movements[:12],
        "paired": paired,
        "failed": failed,
        "featured": featured,
        "total": sum(counts.values()),
        "start": start,
        "end": end,
        "previous_start": previous_start,
    }


def update_rank_alert(session, run, app_id, data):
    if run.params.get("source") not in STORE_SOURCES:
        return
    state = session.get(RankAlertState, (run.monitor_id, app_id))
    if state is None:
        state = RankAlertState(monitor_id=run.monitor_id, app_id=app_id, data={})
        session.add(state)
        prior = session.scalar(
            select(Observation)
            .join(Run)
            .where(
                Run.monitor_id == run.monitor_id,
                Observation.app_id == app_id,
                Run.id != run.id,
                Run.status == "success",
                Observation.retrospective.is_(False),
            )
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(1)
        )
        state.data = {"baseline": prior.data.get("position")} if prior else {}
    value = dict(state.data)
    if value.get("last_run") == run.id:
        return
    value["last_run"] = run.id
    if run.status != "success":
        value["pending"] = 0
        state.data = value
        return
    position, baseline = data.get("position"), value.get("baseline")
    if not position and (
        data.get("found") or (baseline and data.get("results_checked", 0) < baseline)
    ):
        # An unranked placement or a shallow search cannot establish recovery or loss.
        value["pending"] = 0
        state.data = value
        return
    lost = bool(
        baseline
        and baseline <= 10
        and (
            (position and (position > 10 or position - baseline >= 5))
            or (
                not position
                and not data.get("found")
                and data.get("results_checked", 0) >= baseline
            )
        )
    )
    if lost:
        value["pending"] = value.get("pending", 0) + 1
        if value["pending"] >= 2 and not value.get("notified"):
            session.add(
                Notification(
                    app_id=app_id,
                    run_id=run.id,
                    data={
                        "before": baseline,
                        "after": position,
                        "query": run.params["query"],
                        "source": run.params["source"],
                        "country": run.params["country"],
                        "language": run.params.get("language", ""),
                        "results_checked": data.get("results_checked"),
                    },
                )
            )
            value["notified"] = True
    else:
        value = {"baseline": position, "last_run": run.id, "pending": 0, "notified": False}
    state.data = value


def query_matrix(service, monitor_id):
    from .service import observation_record, record

    with service.db.session() as session:
        selected = session.get(Monitor, monitor_id)
        if selected is None:
            raise ValueError("Search not found.")
        monitors = list(session.scalars(select(Monitor).where(Monitor.query == selected.query)))
        monitor_ids = [m.id for m in monitors]
        targets = list(session.scalars(select(Target).where(Target.monitor_id.in_(monitor_ids))))
        apps = list(
            session.scalars(
                select(App).where(App.id.in_([t.app_id for t in targets]), App.archived.is_(False))
            )
        )
        active = {app.id for app in apps}
        # Retrieve at most two observations per pair without reading large saved payloads.
        from sqlalchemy import func

        ranked = (
            select(
                Observation.run_id,
                Observation.app_id,
                func.row_number()
                .over(
                    partition_by=(Run.monitor_id, Observation.app_id),
                    order_by=(func.coalesce(Run.started_at, Run.created_at).desc(), Run.id.desc()),
                )
                .label("n"),
            )
            .join(Run)
            .where(Run.monitor_id.in_(monitor_ids))
            .subquery()
        )
        rows = session.execute(
            select(Observation, Run)
            .join(Run)
            .join(
                ranked,
                (ranked.c.run_id == Observation.run_id) & (ranked.c.app_id == Observation.app_id),
            )
            .where(ranked.c.n <= 2)
        ).all()
        return {
            "query": selected.query,
            "source": selected.source,
            "apps": [{"id": app.id, "name": app.name} for app in apps],
            "monitors": [
                {
                    **record(m),
                    "app_ids": [
                        t.app_id for t in targets if t.monitor_id == m.id and t.app_id in active
                    ],
                }
                for m in monitors
            ],
            "observations": [observation_record(obs, run) for obs, run in rows],
        }
