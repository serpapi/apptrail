import copy
import sqlite3

import pytest
from conftest import FakeGateway, add_monitor
from sqlalchemy import select

from apptrail.db import App, Database, ListingWatch, now
from apptrail.engines import ProviderError
from apptrail.insights import ranking_distribution
from apptrail.listing_history import allowed_asset, archive_media, changed_fields, snapshot_content


def listing_id(client, platform="ios"):
    return next(
        item["id"]
        for item in client.get("/api/state").json()["apps"][0]["listings"]
        if item["platform"] == platform
    )


def watch(client, country="us", frequency="weekly", platform="ios"):
    response = client.post(
        "/api/listing-watches",
        json={
            "listing_id": listing_id(client, platform),
            "country": country,
            "frequency": frequency,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_listing_history_is_opt_in_and_independent_by_market(client, tracked):
    initial_calls = len(FakeGateway.calls)
    service = client.app.state.service
    service.dispatch()
    assert not client.app.state.worker.process_one()
    assert client.get("/api/listing-watches").json() == {"watches": []}
    assert len(FakeGateway.calls) == initial_calls
    us = watch(client)
    gb = watch(client, "gb", "daily")
    assert us != gb
    assert client.app.state.worker.process_one()
    assert client.app.state.worker.process_one()
    assert not client.app.state.worker.process_one()
    data = client.get("/api/listing-watches").json()["watches"]
    assert {w["country"] for w in data} == {"us", "gb"}
    assert all(w["language"] == "auto" for w in data)
    assert data[0]["next_run_at"] > now() + 6 * 86400
    assert client.get("/api/state").json()["estimated_monthly"]["min"] == 34
    assert client.patch(f"/api/listing-watches/{gb}", json={"enabled": False}).status_code == 200
    assert client.get("/api/state").json()["estimated_monthly"]["min"] == 4
    assert watch(client) == us
    assert not client.app.state.worker.process_one()  # Saving an enabled watch doesn't fetch again.


def test_app_store_watch_accepts_automatic_language_from_the_setup_form(client, tracked):
    payload = {
        "listing_id": listing_id(client),
        "country": "us",
        "language": "auto",
        "frequency": "daily",
    }
    response = client.post("/api/listing-watches", json=payload)
    assert response.status_code == 201, response.text
    watch_id = response.json()["id"]
    saved = client.get("/api/listing-watches").json()["watches"][0]
    assert saved["language"] == "auto" and saved["frequency"] == "daily"
    assert client.app.state.worker.process_one()
    snapshots = client.get(f"/api/listing-watches/{watch_id}/history").json()["snapshots"]
    assert len(snapshots) == 1 and snapshots[0]["baseline"]
    for language in ("auto", "en"):
        repeated = client.post("/api/listing-watches", json={**payload, "language": language})
        assert repeated.status_code == 201 and repeated.json()["id"] == watch_id
    assert not client.app.state.worker.process_one()


@pytest.mark.parametrize("language", ["en", "pt-br", "es-419"])
def test_google_play_watch_preserves_explicit_language(client, tracked, language):
    response = client.post(
        "/api/listing-watches",
        json={"listing_id": listing_id(client, "android"), "country": "us", "language": language},
    )
    assert response.status_code == 201, response.text
    saved = client.get("/api/listing-watches").json()["watches"][0]
    assert saved["language"] == language


@pytest.mark.parametrize("language", ["auto", "", "en_US"])
def test_google_play_watch_rejects_automatic_or_invalid_language(client, tracked, language):
    before = list(FakeGateway.calls)
    response = client.post(
        "/api/listing-watches",
        json={"listing_id": listing_id(client, "android"), "country": "us", "language": language},
    )
    assert response.status_code == 422
    if language == "auto":
        assert "Google Play language code" in response.json()["detail"]
    assert client.get("/api/listing-watches").json()["watches"] == []
    assert not client.app.state.worker.process_one()
    assert FakeGateway.calls == before


def test_history_baseline_diffs_and_unchanged_checks(client, tracked, monkeypatch):
    original = FakeGateway.product
    current = {"description": "Plan tasks", "title": "Todo Example"}

    def product(self, *args):
        value = original(self, *args)
        value["title"] = current["title"]
        self.responses = [{"data": copy.deepcopy(current)}]
        return value

    monkeypatch.setattr(FakeGateway, "product", product)
    id = watch(client)
    worker = client.app.state.worker
    assert worker.process_one()
    baseline = client.get(f"/api/listing-watches/{id}/history").json()["snapshots"][0]
    assert baseline["baseline"] and baseline["changes"] == []
    assert client.get(f"/api/listing-snapshots/{baseline['id']}").json()["previous"] is None
    client.post(f"/api/listing-watches/{id}/check")
    worker.process_one()
    assert len(client.get(f"/api/listing-watches/{id}/history").json()["snapshots"]) == 1
    assert (
        len(
            client.get(f"/api/listing-watches/{id}/history?include_unchanged=true").json()[
                "snapshots"
            ]
        )
        == 2
    )
    current["description"] = "Plan tasks together"
    client.post(f"/api/listing-watches/{id}/check")
    worker.process_one()
    latest = client.get(f"/api/listing-watches/{id}/history").json()["snapshots"][0]
    comparison = client.get(f"/api/listing-snapshots/{latest['id']}").json()
    assert latest["changes"] == ["description"]
    assert comparison["previous"]["data"]["description"] == "Plan tasks"
    assert comparison["snapshot"]["data"]["description"] == "Plan tasks together"
    assert client.get("/api/state").json()["apps"][0]["listings"][0]["country"] == "us"


def test_pausing_and_archiving_cancel_collection_and_keep_history(client, tracked):
    id = watch(client)
    worker = client.app.state.worker
    worker.process_one()
    client.post(f"/api/listing-watches/{id}/check")
    client.patch(f"/api/listing-watches/{id}", json={"enabled": False})
    assert not worker.process_one()
    assert client.post(f"/api/listing-watches/{id}/check").status_code == 422
    assert len(client.get(f"/api/listing-watches/{id}/history").json()["snapshots"]) == 1
    client.patch(f"/api/listing-watches/{id}", json={"enabled": True})
    client.patch(f"/api/apps/{tracked}", json={"name": "Todo Example", "archived": True})
    assert not worker.process_one()
    assert client.get("/api/state").json()["listing_monthly_credits"] == 0
    assert client.patch(f"/api/listing-watches/{id}", json={"enabled": True}).status_code == 422


def test_history_failures_do_not_become_baselines_or_diffs(client, tracked, monkeypatch):
    def fail(*args):
        raise ProviderError("Store temporarily unavailable")

    monkeypatch.setattr(FakeGateway, "product", fail)
    id = watch(client)
    assert client.app.state.worker.process_one()
    assert client.get(f"/api/listing-watches/{id}/history").json()["snapshots"] == []
    item = client.get("/api/listing-watches").json()["watches"][0]
    assert item["status"] == "error" and item["next_run_at"] > now()


def test_scheduled_collection_and_deduplicated_pending_jobs(client, tracked):
    id = watch(client)
    client.post(f"/api/listing-watches/{id}/check")
    client.app.state.service.dispatch()
    worker = client.app.state.worker
    assert worker.process_one()
    assert not worker.process_one()
    with client.app.state.service.db.session.begin() as session:
        session.get(ListingWatch, id).next_run_at = now() - 1
    client.app.state.service.dispatch()
    assert worker.process_one()
    assert not worker.process_one()


def test_cancel_during_history_fetch_does_not_save_a_snapshot(client, tracked, monkeypatch):
    id = watch(client)
    original = FakeGateway.product

    def pause(self, *args):
        client.patch(f"/api/listing-watches/{id}", json={"enabled": False})
        return original(self, *args)

    monkeypatch.setattr(FakeGateway, "product", pause)
    assert client.app.state.worker.process_one()
    assert client.get(f"/api/listing-watches/{id}/history").json()["snapshots"] == []


def test_store_normalization_and_immutable_assets(monkeypatch):
    product = {"title": "App", "platform": "ios", "icon": "https://is1-ssl.mzstatic.com/icon.png"}
    raw = {
        "description": "Useful app",
        "iphone_screenshots": ["https://is1-ssl.mzstatic.com/one.png"],
        "version_history": [{"release_version": "2.0", "release_notes": "New features"}],
    }
    content = snapshot_content(product, raw)
    assert content["version"] == "2.0"
    assert content["iphone_screenshots"] == raw["iphone_screenshots"]
    monkeypatch.setattr(
        "apptrail.listing_history.fetch_asset",
        lambda url: {"id": "a" * 64, "mime": "image/png", "content": b"saved image"},
    )
    archived, assets = archive_media(content)
    assert archived["icon"]["asset_id"] == "a" * 64 and assets
    other = copy.deepcopy(archived)
    other["icon"]["url"] = "https://is2-ssl.mzstatic.com/new-url.png"
    assert changed_fields(archived, other) == []  # Same image at a different CDN URL.
    other["iphone_screenshots"] = []
    assert changed_fields(archived, other) == ["iphone_screenshots"]
    failed_image = copy.deepcopy(archived)
    failed_image["icon"]["asset_id"] = None
    assert changed_fields(archived, failed_image) == []


def test_google_listing_normalizes_description_price_and_creatives():
    data = snapshot_content(
        {"platform": "android", "title": "App", "icon": ""},
        {
            "product_info": {"offers": [{"text": "Install"}]},
            "about_this_app": {"snippet": "All your tasks together"},
            "media": {"images": ["https://play-lh.googleusercontent.com/screenshot"]},
            "what_s_new": "Shared projects",
        },
    )
    assert data["price"] == "Free"
    assert data["description"] == "All your tasks together"
    assert len(data["screenshots"]) == 1
    assert data["release_notes"] == "Shared projects"


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/a",
        "http://is1.mzstatic.com/a",
        "https://mzstatic.com.evil.test/a",
        "https://evil.test/mzstatic.com",
        "https://user@mzstatic.com/a",
        "https://mzstatic.com:8443/a",
        "file:///tmp/a",
    ],
)
def test_asset_archive_rejects_untrusted_hosts(url):
    assert not allowed_asset(url)


def test_image_history_is_saved_in_database_and_served_with_auth(client, tracked, monkeypatch):
    original = FakeGateway.product

    def product(self, *args):
        result = original(self, *args)
        result["icon"] = "https://is1-ssl.mzstatic.com/icon.png"
        return result

    monkeypatch.setattr(FakeGateway, "product", product)
    monkeypatch.setattr(
        "apptrail.listing_history.fetch_asset",
        lambda url: {"id": "b" * 64, "mime": "image/png", "content": b"original pixels"},
    )
    id = watch(client)
    client.app.state.worker.process_one()
    asset = client.get("/api/listing-assets/" + "b" * 64)
    assert asset.content == b"original pixels" and asset.headers["content-type"] == "image/png"
    assert client.get(f"/api/listing-watches/{id}/history").json()["snapshots"]
    client.post("/api/auth/logout")
    assert client.get("/api/listing-assets/" + "b" * 64).status_code == 401


def test_notifications_require_confirmed_losses_and_rearm_after_recovery(
    client, tracked, monkeypatch
):
    original = FakeGateway.search
    current = {"position": 3, "fail": False}

    def search(self, spec):
        if current["fail"]:
            raise ProviderError("Temporary failure")
        outcome = original(self, spec)
        outcome.result["items"][0]["position"] = current["position"]
        return outcome

    monkeypatch.setattr(FakeGateway, "search", search)
    id = add_monitor(client, tracked)
    worker = client.app.state.worker
    worker.process_one()

    def check(position, fail=False):
        current.update(position=position, fail=fail)
        client.post(f"/api/monitors/{id}/check")
        worker.process_one()
        return client.get("/api/notifications").json()["notifications"]

    assert check(8) == []
    assert check(9, fail=True) == []
    assert check(9) == []
    alerts = check(10)
    assert len(alerts) == 1 and alerts[0]["data"]["before"] == 3
    assert len(check(15)) == 1
    assert client.get("/api/state").json()["notification_unread"] == 1
    client.patch(f"/api/notifications/{alerts[0]['id']}", json={"action": "read"})
    assert client.get("/api/state").json()["notification_unread"] == 0
    check(3)
    check(12)
    assert len(check(13)) == 2
    client.patch(f"/api/notifications/{alerts[0]['id']}", json={"action": "dismiss"})
    assert len(client.get("/api/notifications").json()["notifications"]) == 1


def test_matrix_uses_saved_checks_and_preserves_country_and_failure(client, tracked):
    us = add_monitor(client, tracked)
    gb = add_monitor(client, tracked, country="gb")
    worker = client.app.state.worker
    worker.process_one()
    FakeGateway.failures = [ProviderError("Search failed")]
    worker.process_one()
    count = len(FakeGateway.calls)
    response = client.get(f"/api/monitors/{us}/matrix")
    data = response.json()
    assert {m["id"] for m in data["monitors"]} == {us, gb}
    assert {row["status"] for row in data["observations"]} == {"success", "error"}
    assert len(FakeGateway.calls) == count


@pytest.mark.parametrize(
    "positions,checked,expected",
    [
        ([9, 11, 12], 100, 1),
        ([3, 7, 7], 100, 0),
        ([40, 80, 85], 100, 0),
        ([3, None, None], 100, 1),
        ([9, None, None], 5, 0),
    ],
)
def test_alert_thresholds_and_search_coverage(
    client, tracked, monkeypatch, positions, checked, expected
):
    original = FakeGateway.search
    pending = iter(positions)

    def search(self, spec):
        outcome = original(self, spec)
        position = next(pending)
        if position is None:
            outcome.result["items"] = []
        else:
            outcome.result["items"][0]["position"] = position
        outcome.result["results_checked"] = checked
        return outcome

    monkeypatch.setattr(FakeGateway, "search", search)
    monitor = add_monitor(client, tracked)
    worker = client.app.state.worker
    worker.process_one()
    for _ in positions[1:]:
        client.post(f"/api/monitors/{monitor}/check")
        worker.process_one()
    assert len(client.get("/api/notifications").json()["notifications"]) == expected


def test_distribution_uses_same_cohort_and_does_not_count_repeated_checks():
    def row(id, time, position, status="success", found=None):
        return {
            "monitor_id": id,
            "app_id": 1,
            "checked_at": time,
            "run_id": time,
            "params": {"source": "apple_app_store"},
            "status": status,
            "data": {
                "position": position,
                "found": position is not None if found is None else found,
            },
        }

    d = ranking_distribution(
        [
            row(1, 10, 3),
            row(1, 21, 10),
            row(1, 22, 20),
            row(2, 12, 5),
            row(2, 25, 0, "error"),
            row(3, 27, None),
            row(4, 28, None, found=True),
        ],
        20,
        30,
    )
    assert d["total"] == 2 and d["counts"]["top50"] == 1 and d["counts"]["not_found"] == 1
    assert d["paired"] == 1 and d["deltas"]["top3"] == -1 and d["deltas"]["top50"] == 1
    assert d["failed"] == 1 and d["featured"] == 1


def test_schema_four_upgrade_preserves_existing_data_and_disables_history(tmp_path):
    db = Database(tmp_path)
    with db.session.begin() as session:
        session.add(App(name="Existing app"))
    db.close()
    with sqlite3.connect(tmp_path / "apptrail.sqlite3") as c:
        c.execute("DROP INDEX one_active_listing_watch")
        c.execute("ALTER TABLE runs DROP COLUMN watch_id")
        for name in (
            "listing_snapshots",
            "listing_watches",
            "listing_assets",
            "notifications",
            "rank_alert_states",
        ):
            c.execute(f"DROP TABLE {name}")
        c.execute("PRAGMA user_version=4")
    upgraded = Database(tmp_path)
    with upgraded.session() as session:
        assert session.scalar(select(App)).name == "Existing app"
        assert list(session.scalars(select(ListingWatch))) == []
    assert (tmp_path / "backups/before-schema-4.sqlite3").exists()
    upgraded.close()
