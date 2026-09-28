import pytest
from conftest import FakeGateway, add_monitor
from sqlalchemy import select

from apptrail.db import Database, ListingWatch, Monitor, Run
from apptrail.engines import ProviderError
from apptrail.service import Service


def test_group_schedule_updates_all_variants_without_resuming_paused_checks(client, tracked):
    ids = [
        add_monitor(client, tracked, source=source, country=country)
        for source in ("apple_app_store", "google_play")
        for country in ("us", "gb")
    ]
    other = add_monitor(client, tracked, query="habit tracker")
    client.patch(f"/api/monitors/{ids[0]}", json={"enabled": False})
    response = client.patch("/api/monitor-groups", json={"monitor_ids": ids, "frequency": "weekly"})
    assert response.status_code == 200, response.text
    state = client.get("/api/state").json()
    grouped = [item for item in state["monitors"] if item["id"] in ids]
    assert {item["frequency"] for item in grouped} == {"weekly"}
    assert [item["id"] for item in grouped if not item["enabled"]] == [ids[0]]
    assert next(item for item in state["monitors"] if item["id"] == other)["frequency"] == "daily"


@pytest.mark.parametrize("invalid", ["missing", "different-query", "different-kind"])
def test_invalid_group_is_rejected_without_partial_changes(client, tracked, invalid):
    first = add_monitor(client, tracked)
    second = (
        99999
        if invalid == "missing"
        else add_monitor(client, tracked, query="another query")
        if invalid == "different-query"
        else add_monitor(client, tracked, source="google_ai_mode")
    )
    response = client.patch(
        "/api/monitor-groups",
        json={"monitor_ids": [first, second], "frequency": "monthly", "enabled": False},
    )
    assert response.status_code == 422
    with client.app.state.service.db.session() as session:
        monitor = session.get(Monitor, first)
        assert monitor.frequency == "daily" and monitor.enabled
        assert session.scalar(select(Run).where(Run.monitor_id == first)).status == "queued"


def test_group_pause_resume_and_manual_checks_are_deduplicated(client, tracked):
    ids = [
        add_monitor(client, tracked, source=source) for source in ("google_ai_mode", "bing_copilot")
    ]
    for enabled in (False, True):
        assert (
            client.patch(
                "/api/monitor-groups", json={"monitor_ids": ids, "enabled": enabled}
            ).status_code
            == 200
        )
        with client.app.state.service.db.session() as session:
            assert all(item.enabled == enabled for item in session.scalars(select(Monitor)))
            pending = session.scalars(select(Run).where(Run.status == "queued")).all()
            assert len(pending) == (2 if enabled else 0)
    first = client.post("/api/monitor-groups/check", json={"monitor_ids": ids}).json()
    repeated = client.post("/api/monitor-groups/check", json={"monitor_ids": ids + ids}).json()
    assert len(first["run_ids"]) == 2 and repeated == first


def test_workspace_pause_holds_every_job_kind_and_preserves_individual_settings(client, tracked):
    ids = [
        add_monitor(client, tracked, source=source)
        for source in ("apple_app_store", "google_ai_mode")
    ]
    disabled = add_monitor(client, tracked, query="paused query", frequency="monthly")
    client.patch(f"/api/monitors/{disabled}", json={"enabled": False})
    state = client.get("/api/state").json()
    listing = state["apps"][0]["listings"][0]["id"]
    watch = client.post("/api/listing-watches", json={"listing_id": listing, "country": "us"})
    assert watch.status_code == 201, watch.text
    client.post(f"/api/apps/{tracked}/refresh")
    assert client.patch("/api/sync", json={"paused": True}).json() == {"paused": True}
    service, worker = client.app.state.service, client.app.state.worker
    calls = len(FakeGateway.calls)
    for _ in range(2):
        service.dispatch()
        assert not worker.process_one()
    assert len(FakeGateway.calls) == calls
    assert client.get("/api/state").json()["sync_paused"] is True
    with service.db.session() as session:
        pending = session.scalars(select(Run).where(Run.status == "queued")).all()
        assert {item.kind for item in pending} == {"search", "profile", "listing_history"}
        assert all(item.attempts == 0 for item in pending)
        assert session.scalar(select(ListingWatch)).enabled
    reopened = Database(service.config.directory)
    try:
        assert Service(reopened, service.config, FakeGateway).sync_paused()
    finally:
        reopened.engine.dispose()
    assert client.patch("/api/sync", json={"paused": False}).status_code == 200
    while worker.process_one():
        pass
    with service.db.session() as session:
        assert all(session.get(Monitor, id).enabled for id in ids)
        assert session.get(Monitor, disabled).enabled is False
        assert session.get(Monitor, disabled).frequency == "monthly"
        assert not session.scalar(select(Run).where(Run.status.in_(["queued", "running"])))
        assert {
            run.kind for run in session.scalars(select(Run).where(Run.status == "success"))
        } == {"search", "profile", "listing_history"}


def test_pause_holds_retry_from_inflight_job_until_resume(client, tracked, monkeypatch):
    monitor = add_monitor(client, tracked)
    original_search = FakeGateway.search

    def fail_while_pausing(gateway, spec):
        gateway.requests_count = 1
        assert client.patch("/api/sync", json={"paused": True}).status_code == 200
        raise ProviderError("Try again later", retryable=True)

    monkeypatch.setattr(FakeGateway, "search", fail_while_pausing)
    worker = client.app.state.worker
    assert worker.process_one()
    with client.app.state.service.db.session.begin() as session:
        run = session.scalar(select(Run).where(Run.monitor_id == monitor))
        assert run.status == "queued" and run.attempts == 1
        run.available_at = 0
    assert not worker.process_one()
    monkeypatch.setattr(FakeGateway, "search", original_search)
    client.patch("/api/sync", json={"paused": False})
    assert worker.process_one()
    with client.app.state.service.db.session() as session:
        run = session.scalar(select(Run).where(Run.monitor_id == monitor))
        assert run.status == "success" and run.attempts == 2


def test_paused_dispatch_does_not_enqueue_due_jobs_but_resume_does(client, tracked):
    monitor = add_monitor(client, tracked)
    worker, service = client.app.state.worker, client.app.state.service
    assert worker.process_one()
    client.patch("/api/sync", json={"paused": True})
    with service.db.session.begin() as session:
        session.get(Monitor, monitor).next_run_at = 0
    service.dispatch()
    with service.db.session() as session:
        assert not session.scalar(select(Run).where(Run.status == "queued"))
    client.patch("/api/sync", json={"paused": False})
    assert worker.process_one()
