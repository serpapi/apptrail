import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import FakeGateway, add_monitor
from sqlalchemy import event, select

from apptrail.db import (
    App,
    Competitor,
    Listing,
    ListingWatch,
    Monitor,
    Observation,
    Run,
    Target,
    now,
)
from apptrail.service import next_check


def test_partial_app_edit_preserves_archived_state_and_matching_fields(client, tracked):
    assert (
        client.patch(
            f"/api/apps/{tracked}",
            json={
                "name": "Todo Example",
                "aliases": ["Todo"],
                "website": "https://example.com/todo",
                "archived": True,
            },
        ).status_code
        == 200
    )
    assert client.patch(f"/api/apps/{tracked}", json={"name": "Renamed app"}).status_code == 200
    saved = client.get("/api/state").json()["apps"][0]
    assert saved["name"] == "Renamed app"
    assert saved["archived"] is True
    assert saved["aliases"] == ["Todo"]
    assert saved["website"] == "https://example.com/todo"
    assert (
        client.patch(
            f"/api/apps/{tracked}",
            json={"name": "Renamed app", "archived": False, "aliases": [], "website": ""},
        ).status_code
        == 200
    )
    saved = client.get("/api/state").json()["apps"][0]
    assert saved["archived"] is False and saved["aliases"] == [] and saved["website"] == ""


def test_concurrent_duplicate_batches_reuse_one_monitor_and_job_per_variant(client, tracked):
    engine = client.app.state.service.db.engine
    second_read = threading.Event()
    count_lock = threading.Lock()
    reads = 0

    def allow_overlapping_reads(connection, cursor, statement, parameters, context, executemany):
        nonlocal reads
        if statement.startswith("SELECT monitors.") and "WHERE monitors.signature =" in statement:
            with count_lock:
                reads += 1
                current = reads
            if current == 1:
                second_read.wait(0.2)
            elif current == 2:
                second_read.set()

    body = {
        "monitors": [
            {"query": "shared task query", "source": source, "app_ids": [tracked]}
            for source in ("apple_app_store", "google_play")
        ]
    }
    start = threading.Barrier(2)

    def create():
        start.wait(timeout=5)
        return client.post("/api/monitors/batch", json=body)

    event.listen(engine, "after_cursor_execute", allow_overlapping_reads)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create) for _ in range(2)]
            responses = [future.result(timeout=10) for future in futures]
    finally:
        event.remove(engine, "after_cursor_execute", allow_overlapping_reads)
    assert [response.status_code for response in responses] == [201, 201]
    results = [response.json()["monitors"] for response in responses]
    assert [item["id"] for item in results[0]] == [item["id"] for item in results[1]]
    assert sorted(sum(not item["reused"] for item in result) for result in results) == [0, 2]
    with client.app.state.service.db.session() as session:
        assert len(session.scalars(select(Monitor)).all()) == 2
        assert len(session.scalars(select(Run)).all()) == 2
        assert len(session.scalars(select(Target)).all()) == 2


@pytest.mark.parametrize("method", ["post", "patch"])
def test_listing_frequency_changes_use_last_success_and_resaving_keeps_due_date(
    client, tracked, method
):
    service, worker = client.app.state.service, client.app.state.worker
    listing_id = client.get("/api/state").json()["apps"][0]["listings"][0]["id"]
    body = {"listing_id": listing_id, "country": "us", "frequency": "weekly"}
    response = client.post("/api/listing-watches", json=body)
    assert response.status_code == 201
    watch_id = response.json()["id"]
    assert worker.process_one()
    finished = now() - 6 * 86400
    with service.db.session.begin() as session:
        successful = session.scalar(select(Run).where(Run.watch_id == watch_id))
        successful.started_at = successful.finished_at = finished
        session.get(ListingWatch, watch_id).next_run_at = next_check(finished, "weekly")
        session.add(
            Run(
                kind="listing_history",
                watch_id=watch_id,
                status="error",
                finished_at=now() - 86400,
                params={},
            )
        )

    def save(frequency):
        return (
            client.post("/api/listing-watches", json={**body, "frequency": frequency})
            if method == "post"
            else client.patch(f"/api/listing-watches/{watch_id}", json={"frequency": frequency})
        )

    assert save("daily").status_code in {200, 201}
    with service.db.session() as session:
        due = session.get(ListingWatch, watch_id).next_run_at
    assert due == next_check(finished, "daily") < now()
    for _ in range(2):
        assert save("daily").status_code in {200, 201}
        with service.db.session() as session:
            assert session.get(ListingWatch, watch_id).next_run_at == due
    service.dispatch()
    assert worker.process_one()
    assert not worker.process_one()


def test_listing_schedule_change_before_first_success_stays_due(client, tracked):
    listing_id = client.get("/api/state").json()["apps"][0]["listings"][0]["id"]
    response = client.post("/api/listing-watches", json={"listing_id": listing_id, "country": "us"})
    watch_id = response.json()["id"]
    assert (
        client.patch(f"/api/listing-watches/{watch_id}", json={"frequency": "monthly"}).status_code
        == 200
    )
    with client.app.state.service.db.session() as session:
        assert session.get(ListingWatch, watch_id).next_run_at <= now()
    assert client.app.state.worker.process_one()


@pytest.mark.parametrize("archived_parent", [False, True])
def test_unlink_competitor_preserves_app_shared_tracking_history_and_other_parents(
    client, tracked, archived_parent
):
    service, worker = client.app.state.service, client.app.state.worker
    with service.db.session.begin() as session:
        rival = App(name="Rival")
        another = App(name="Another original")
        session.add_all([rival, another])
        session.flush()
        rival_id, another_id = rival.id, another.id
        session.add(
            Listing(
                app_id=rival_id,
                platform="ios",
                external_id="999",
                title="Rival",
                url="https://apps.apple.com/us/app/id999",
            )
        )
        session.add(Competitor(app_id=another_id, competitor_id=rival_id))
    monitor = add_monitor(client, tracked)
    assert worker.process_one()
    assert (
        client.post(
            f"/api/apps/{tracked}/competitors",
            json={"existing_app_id": rival_id, "monitor_ids": [monitor]},
        ).status_code
        == 201
    )
    client.post(f"/api/monitors/{monitor}/check")
    if archived_parent:
        assert (
            client.patch(
                f"/api/apps/{tracked}", json={"name": "Todo Example", "archived": True}
            ).status_code
            == 200
        )
    before = client.get("/api/state").json()
    calls = list(FakeGateway.calls)
    for _ in range(2):
        response = client.delete(f"/api/apps/{tracked}/competitors/{rival_id}")
        assert response.status_code == 200 and response.json() == {"ok": True}
    after = client.get("/api/state").json()
    assert after["monitors"] == before["monitors"]
    assert after["latest_observations"] == before["latest_observations"]
    assert after["jobs"] == before["jobs"]
    saved = next(app for app in after["apps"] if app["id"] == rival_id)
    assert saved["competitor_for"] == [another_id] and not saved["archived"]
    assert FakeGateway.calls == calls
    with service.db.session() as session:
        assert session.get(Target, (monitor, rival_id)) is not None
        assert session.scalar(select(Observation).where(Observation.app_id == rival_id))
        assert session.get(Competitor, (tracked, rival_id)) is None
    assert worker.process_one()


def test_unlink_competitor_requires_existing_apps_and_csrf(client, tracked):
    assert client.delete(f"/api/apps/{tracked}/competitors/999999").status_code == 404
    assert client.delete(f"/api/apps/999999/competitors/{tracked}").status_code == 404
    assert (
        client.delete(
            f"/api/apps/{tracked}/competitors/{tracked}", headers={"X-CSRF-Token": ""}
        ).status_code
        == 403
    )


def test_google_play_numeric_region_language_works_for_discovery_and_tracking(client, tracked):
    response = client.post(
        "/api/discover", json={"platform": "android", "query": "Todo", "language": "es-419"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["candidates"][0]["language"] == "es-419"
    monitor = add_monitor(client, tracked, source="google_play", language="es-419")
    saved = next(
        item for item in client.get("/api/state").json()["monitors"] if item["id"] == monitor
    )
    assert saved["language"] == "es-419"
    assert (
        client.post(
            "/api/discover", json={"platform": "ios", "query": "Todo", "language": "es-419"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/monitors",
            json={
                "source": "apple_app_store",
                "query": "Todo",
                "language": "es-419",
                "app_ids": [tracked],
            },
        ).status_code
        == 422
    )
