import pytest
from conftest import FakeGateway, add_monitor
from sqlalchemy import select

from apptrail.db import App, Listing, RankAlertState, Run
from apptrail.engines import ProviderError


def enable_listing_history(client, app_id):
    app = next(app for app in client.get("/api/state").json()["apps"] if app["id"] == app_id)
    listing_id = next(item["id"] for item in app["listings"] if item["platform"] == "ios")
    payload = {"listing_id": listing_id, "country": "us"}
    response = client.post("/api/listing-watches", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"], payload


@pytest.mark.parametrize("action", ["resume", "enable", "check_after_restore"])
@pytest.mark.parametrize("retry", [False, True])
def test_explicit_listing_restart_keeps_inflight_result(
    client, tracked, monkeypatch, action, retry
):
    watch_id, payload = enable_listing_history(client, tracked)
    worker = client.app.state.worker
    original_product = FakeGateway.product
    calls_before = len(FakeGateway.calls)

    def restart_during_product(gateway, *args):
        product = original_product(gateway, *args)
        if action == "check_after_restore":
            for archived in (True, False):
                response = client.patch(
                    f"/api/apps/{tracked}",
                    json={"name": "Todo Example", "archived": archived},
                )
                assert response.status_code == 200
            response = client.post(f"/api/listing-watches/{watch_id}/check")
        else:
            response = client.patch(f"/api/listing-watches/{watch_id}", json={"enabled": False})
            assert response.status_code == 200
            response = (
                client.patch(f"/api/listing-watches/{watch_id}", json={"enabled": True})
                if action == "resume"
                else client.post("/api/listing-watches", json=payload)
            )
        assert response.status_code in (200, 201), response.text
        if retry:
            raise ProviderError("Temporary provider failure", retryable=True)
        return product

    monkeypatch.setattr(FakeGateway, "product", restart_during_product)
    assert worker.process_one()
    if retry:
        with client.app.state.service.db.session.begin() as session:
            run = session.scalar(select(Run))
            assert run.status == "queued"
            run.available_at = 0
        monkeypatch.setattr(FakeGateway, "product", original_product)
        assert worker.process_one()

    snapshots = client.get(f"/api/listing-watches/{watch_id}/history").json()["snapshots"]
    assert len(snapshots) == 1
    details = client.get(f"/api/listing-snapshots/{snapshots[0]['id']}").json()
    assert details["snapshot"]["data"]["title"] == "Todo Example"
    run = client.get(f"/api/runs/{snapshots[0]['run_id']}").json()
    assert run["status"] == "success"
    assert run["attempts"] == run["requests_count"] == (2 if retry else 1)
    assert len(FakeGateway.calls) - calls_before == (2 if retry else 1)
    assert not worker.process_one()


def test_scheduled_dispatch_does_not_undo_listing_cancellation(client, tracked, monkeypatch):
    watch_id, _ = enable_listing_history(client, tracked)
    original_product = FakeGateway.product

    def archive_during_product(gateway, *args):
        for archived in (True, False):
            response = client.patch(
                f"/api/apps/{tracked}",
                json={"name": "Todo Example", "archived": archived},
            )
            assert response.status_code == 200
        client.app.state.service.dispatch()
        return original_product(gateway, *args)

    monkeypatch.setattr(FakeGateway, "product", archive_during_product)
    assert client.app.state.worker.process_one()
    assert client.get(f"/api/listing-watches/{watch_id}/history").json()["snapshots"] == []
    watch = client.get("/api/listing-watches").json()["watches"][0]
    assert watch["enabled"] and watch["status"] == "cancelled"
    assert not client.app.state.worker.process_one()


@pytest.mark.parametrize("fail", [False, True])
def test_shared_search_preserves_archived_app_history_and_alerts(
    client, tracked, monkeypatch, fail
):
    monitor_id = add_monitor(client, tracked)
    service = client.app.state.service
    with service.db.session.begin() as session:
        active = App(name="Active competitor")
        session.add(active)
        session.flush()
        active_id = active.id
        session.add(
            Listing(
                app_id=active_id,
                platform="ios",
                external_id="654321",
                title="Active competitor",
                url="https://apps.apple.com/us/app/id654321",
            )
        )
    attached = client.post(f"/api/monitors/{monitor_id}/targets", json={"app_ids": [active_id]})
    assert attached.status_code == 200
    worker = client.app.state.worker
    assert worker.process_one()
    history_before = client.get(f"/api/dashboard?app_id={tracked}").json()["observations"]
    with service.db.session() as session:
        alert_before = dict(session.get(RankAlertState, (monitor_id, tracked)).data)
    original_search = FakeGateway.search

    def archive_during_search(gateway, spec):
        response = client.patch(
            f"/api/apps/{tracked}", json={"name": "Todo Example", "archived": True}
        )
        assert response.status_code == 200
        if fail:
            gateway.requests_count = 1
            raise ProviderError("Provider unavailable")
        result = original_search(gateway, spec)
        result.result["items"][0]["position"] = 15
        return result

    monkeypatch.setattr(FakeGateway, "search", archive_during_search)
    for _ in range(2):
        response = client.post(f"/api/monitors/{monitor_id}/check")
        assert response.status_code == 200
        assert worker.process_one()
        run = client.get(f"/api/runs/{response.json()['run_id']}").json()
        assert run["status"] == ("error" if fail else "success")
        assert [observation["app_id"] for observation in run["observations"]] == [active_id]

    assert client.get(f"/api/dashboard?app_id={tracked}").json()["observations"] == history_before
    assert client.get("/api/notifications").json()["notifications"] == []
    with service.db.session() as session:
        assert session.get(RankAlertState, (monitor_id, tracked)).data == alert_before
    active_history = client.get(f"/api/dashboard?app_id={active_id}").json()["observations"]
    assert len(active_history) == 3
