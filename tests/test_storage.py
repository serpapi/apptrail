import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from conftest import FakeGateway, add_monitor, sign_in
from sqlalchemy import select
from test_backups import restore
from test_insights import watch

from apptrail import storage
from apptrail.db import ListingAsset, ListingSnapshot, Observation, Run, now
from apptrail.engines import ProviderError


def asset_id(name):
    return hashlib.sha256(name.encode()).hexdigest()


@pytest.fixture
def stored_history(client, tracked, monkeypatch):
    timestamp = now()
    monkeypatch.setattr(storage, "now", lambda: timestamp)
    watch_id = watch(client, platform="android")
    runs, snapshots = {}, {}
    db = client.app.state.service.db
    with db.session.begin() as session:
        for age in (40, 10, 7, 3):
            run = Run(
                kind="search",
                status="success",
                created_at=timestamp - age * 86400,
                started_at=timestamp - age * 86400,
                finished_at=timestamp - age * 86400,
                params={"source": "google_play", "query": f"Search {age}", "country": "us"},
                result={"kind": "store", "items": [], "results_checked": 1},
                responses=[{"raw": "response" * 150_000 + "é"}],
            )
            session.add(run)
            session.flush()
            session.add(
                Observation(
                    run_id=run.id,
                    app_id=tracked,
                    data={
                        "position": 1,
                        "found": True,
                        "evidence": [{"type": "app_id", "text": "Matched app"}],
                    },
                )
            )
            session.add(
                ListingAsset(
                    id=asset_id(f"image-{age}"), mime="image/webp", content=b"image" * 100_000
                )
            )
            snapshot = ListingSnapshot(
                watch_id=watch_id,
                run_id=run.id,
                checked_at=run.finished_at,
                data={
                    "title": f"Listing {age}",
                    "description": "Saved listing text",
                    "icon": {"asset_id": asset_id(f"image-{age}"), "pixel_hash": f"pixels-{age}"},
                },
                changes=["title", "icon"],
                baseline=age == 40,
            )
            session.add(snapshot)
            session.flush()
            runs[age], snapshots[age] = run.id, snapshot.id
        session.add(
            ListingAsset(id=asset_id("shared"), mime="image/webp", content=b"shared" * 100_000)
        )
        for age in (40, 3):
            snapshot = session.get(ListingSnapshot, snapshots[age])
            snapshot.data = {
                **snapshot.data,
                "ipad_screenshots": [
                    {"asset_id": asset_id("shared"), "pixel_hash": "shared-pixels"}
                ],
            }
        session.add(ListingAsset(id=asset_id("orphan"), mime="image/webp", content=b"unused"))
        session.add(
            Run(
                kind="profile",
                status="running",
                created_at=timestamp - 60 * 86400,
                responses=[{"active": True}],
            )
        )
    return {"runs": runs, "snapshots": snapshots, "timestamp": timestamp}


def clean(client, kind="responses", days=7):
    response = client.post(
        "/api/storage/cleanup", json={"kind": kind, "days": days, "confirm": True}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_storage_counts_payload_bytes_shared_images_and_cutoffs(client, stored_history):
    usage = client.get("/api/storage").json()
    db = client.app.state.service.db
    with closing(sqlite3.connect(db.path)) as connection:
        expected = connection.execute(
            "SELECT SUM(length(CAST(responses AS BLOB))) FROM run_payloads WHERE responses != '[]'"
        ).fetchone()[0]
    assert usage["responses"]["bytes"] == expected
    assert usage["responses"]["older_than"]["7"]["count"] == 2
    assert usage["responses"]["older_than"]["30"]["count"] == 1
    assert usage["images"]["bytes"] == 2_600_006
    assert usage["images"]["count"] == 6
    assert usage["images"]["older_than"]["7"] == {"bytes": 1_000_006, "count": 3}
    assert usage["images"]["older_than"]["30"] == {"bytes": 500_006, "count": 2}
    assert usage["total_bytes"] == usage["database_bytes"] + usage["journal_bytes"]
    assert usage["total_bytes"] > usage["images"]["bytes"] + expected


@pytest.mark.parametrize("days, removed", [(7, {40, 10}), (30, {40})])
def test_response_cleanup_preserves_results_images_and_active_runs(
    client, stored_history, days, removed
):
    before = client.get("/api/storage").json()
    originals = {
        age: client.get(f"/api/runs/{run_id}").json()
        for age, run_id in stored_history["runs"].items()
    }
    result = clean(client, days=days)
    assert result["removed_count"] == len(removed)
    assert result["removed_bytes"] == before["responses"]["older_than"][str(days)]["bytes"]
    assert result["compacted"]
    assert result["usage"]["total_bytes"] < before["total_bytes"] - result["removed_bytes"] // 2
    assert result["usage"]["images"] == before["images"]
    for age, run_id in stored_history["runs"].items():
        run = client.get(f"/api/runs/{run_id}").json()
        assert bool(run["responses"]) == (age not in removed)
        assert run["responses_cleaned"] == (age in removed)
        assert not run["responses_omitted"]
        assert run["result"] == originals[age]["result"]
        assert run["observations"] == originals[age]["observations"]
    with client.app.state.service.db.session() as session:
        active = session.scalar(select(Run).where(Run.status == "running"))
        assert active.responses == [{"active": True}]
    assert clean(client, days=days)["removed_bytes"] == 0


@pytest.mark.parametrize("days, removed", [(7, {40, 10}), (30, {40})])
def test_image_cleanup_protects_recent_references_and_keeps_hashes(
    client, stored_history, days, removed
):
    original = client.get("/api/storage").json()
    result = clean(client, "images", days)
    assert result["removed_count"] == len(removed) + 1
    assert result["usage"]["responses"] == original["responses"]
    for age, snapshot_id in stored_history["snapshots"].items():
        snapshot = client.get(f"/api/listing-snapshots/{snapshot_id}").json()["snapshot"]
        assert snapshot["images_cleaned"] == (age in removed)
        assert not snapshot["images_omitted"]
        assert snapshot["missing_assets"] == ([asset_id(f"image-{age}")] if age in removed else [])
        assert snapshot["data"]["icon"]["pixel_hash"] == f"pixels-{age}"
        assert snapshot["data"]["description"] == "Saved listing text"
        assert snapshot["changes"] == ["title", "icon"]
    assert client.get(f"/api/listing-assets/{asset_id('shared')}").status_code == 200
    assert client.get(f"/api/listing-assets/{asset_id('orphan')}").status_code == 404
    assert client.get("/api/state").status_code == 200


def test_legacy_responses_and_cleanup_marker_survive_different_cutoffs(client, stored_history):
    db = client.app.state.service.db
    with db.session.begin() as session:
        session.get(Run, stored_history["runs"][10]).legacy_responses = [{"legacy": True}]
    result = clean(client)
    assert result["removed_count"] == 3
    with db.session() as session:
        assert session.get(Run, stored_history["runs"][10]).legacy_responses == []
    clean(client, days=30)
    assert client.get(f"/api/runs/{stored_history['runs'][10]}").json()["responses_cleaned"]


def test_response_removal_notices_only_identify_content_actually_removed(client, stored_history):
    app_id = client.get("/api/state").json()["apps"][0]["id"]
    monitor_id = add_monitor(client, app_id)
    FakeGateway.failures = [ProviderError("Provider returned no response", retryable=False)]
    while client.app.state.worker.process_one():
        pass
    db = client.app.state.service.db
    with db.session.begin() as session:
        failed = session.scalar(select(Run).where(Run.monitor_id == monitor_id))
        assert failed.status == "error" and failed.responses == []
        failed.finished_at = stored_history["timestamp"] - 40 * 86400
        failed_id = failed.id
    cleaned_id = stored_history["runs"][40]
    omitted_id = stored_history["runs"][3]
    clean(client)

    def check_notices(run_id, *, cleaned=False, omitted=False):
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200, response.text
        run = response.json()
        assert run["responses_cleaned"] is cleaned
        assert run["responses_omitted"] is omitted

    check_notices(failed_id)
    check_notices(cleaned_id, cleaned=True)
    check_notices(omitted_id)
    for _ in range(2):
        content = client.get("/api/backup").content
        assert restore(client, content).status_code == 200
        sign_in(client)
        check_notices(failed_id)
        check_notices(cleaned_id, cleaned=True)
        check_notices(omitted_id, omitted=True)
        clean(client, days=30)
        check_notices(failed_id)
        check_notices(cleaned_id, cleaned=True)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "responses", "days": 1, "confirm": True},
        {"kind": "everything", "days": 7, "confirm": True},
        {"kind": "images", "days": 7},
        {"kind": "images", "days": 7, "confirm": False},
    ],
)
def test_cleanup_requires_explicit_scope_and_confirmation(client, stored_history, payload):
    assert client.post("/api/storage/cleanup", json=payload).status_code == 422
    assert client.get("/api/storage").json()["images"]["count"] == 6


def test_storage_requires_authentication_and_csrf(client):
    payload = {"kind": "images", "days": 7, "confirm": True}
    assert (
        client.post(
            "/api/storage/cleanup", json=payload, headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )
    del client.headers["X-CSRF-Token"]
    assert client.post("/api/storage/cleanup", json=payload).status_code == 403
    client.cookies.clear()
    assert client.get("/api/storage").status_code == 401
    assert client.post("/api/storage/cleanup", json=payload).status_code == 401


def test_cleanup_blocks_concurrent_requests_and_recovers_after_failure(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def failing_cleanup(*args):
        entered.set()
        assert release.wait(5)
        raise ValueError("Cleanup failed")

    monkeypatch.setattr(storage, "cleanup", failing_cleanup)
    with ThreadPoolExecutor() as executor:
        future = executor.submit(
            client.post, "/api/storage/cleanup", json={"kind": "images", "days": 7, "confirm": True}
        )
        assert entered.wait(5)
        try:
            response = client.get("/api/state")
            assert response.status_code == 503
            assert "Storage cleanup" in response.json()["detail"]
        finally:
            release.set()
        assert future.result().status_code == 422
    assert client.get("/api/state").status_code == 200
