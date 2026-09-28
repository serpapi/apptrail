import sqlite3
from datetime import UTC, datetime

import pytest
from conftest import TEST_PASSWORD, FakeGateway, add_monitor, sign_in
from fastapi.testclient import TestClient
from sqlalchemy import select

from apptrail.api import create_app
from apptrail.db import Candidate, Listing, Monitor, Run, Setting, now
from apptrail.engines import ProviderError
from apptrail.service import next_check


def test_key_validation_and_secret_storage(client):
    assert client.post("/api/key", json={"api_key": "invalid"}).status_code == 502
    assert not client.get("/api/state").json()["configured"]
    assert client.post("/api/key", json={"api_key": "unit-test-secret"}).status_code == 200
    response = client.get("/api/state")
    assert "unit-test-secret" not in response.text
    path = client.app.state.service.config.secret_path
    assert path.stat().st_mode & 0o777 == 0o600
    assert (
        client.post("/api/key", json={"api_key": {"nested": "unit-test-secret"}}).status_code == 422
    )
    assert (
        "unit-test-secret"
        not in client.post("/api/key", json={"api_key": {"nested": "unit-test-secret"}}).text
    )


def test_onboarding_requires_verified_identifiers(client):
    assert client.post("/api/apps", json={"name": "App", "candidate_tokens": []}).status_code == 422
    assert (
        client.post("/api/apps", json={"name": "App", "candidate_tokens": ["made-up"]}).status_code
        == 422
    )
    candidate = client.post("/api/discover", json={"platform": "ios", "query": "app"}).json()[
        "candidates"
    ][0]
    with client.app.state.service.db.session.begin() as session:
        session.get(Candidate, candidate["token"]).expires_at = 0
    assert (
        client.post(
            "/api/apps", json={"name": "App", "candidate_tokens": [candidate["token"]]}
        ).status_code
        == 422
    )


def test_cross_platform_app_and_duplicate_rejection(client, tracked):
    state = client.get("/api/state").json()
    assert {listing["external_id"] for listing in state["apps"][0]["listings"]} == {
        "123456",
        "org.example.todo",
    }
    candidate = client.post("/api/discover", json={"platform": "ios", "query": "Todo"}).json()[
        "candidates"
    ][0]
    assert (
        client.post(
            "/api/apps", json={"name": "Duplicate", "candidate_tokens": [candidate["token"]]}
        ).status_code
        == 422
    )


@pytest.mark.parametrize("platforms", [("ios", "android"), ("android", "ios")])
def test_onboarding_connects_second_store_to_the_same_app(client, platforms):
    app_id = None
    for index, platform in enumerate(platforms, 1):
        discovery = client.post("/api/discover", json={"platform": platform, "query": "Todo"})
        assert discovery.status_code == 200
        candidate = discovery.json()["candidates"][0]
        response = client.post(
            f"/api/apps/{app_id}/listings" if app_id else "/api/apps",
            json={"name": "Todo Example", "candidate_tokens": [candidate["token"]]},
        )
        assert response.status_code == 201
        if app_id:
            assert response.json()["id"] == app_id
        app_id = response.json()["id"]
        apps = client.get("/api/state").json()["apps"]
        assert len(apps) == 1
        assert len(apps[0]["listings"]) == index
    assert {listing["platform"] for listing in apps[0]["listings"]} == {"ios", "android"}


def test_queue_deduplicates_and_saves_history(client, tracked):
    monitor_id = add_monitor(client, tracked)
    first = client.post(f"/api/monitors/{monitor_id}/check").json()
    assert client.post(f"/api/monitors/{monitor_id}/check").json() == first
    assert client.app.state.worker.process_one()
    result = client.get(f"/api/runs/{first['run_id']}").json()
    assert result["status"] == "success"
    assert result["observations"][0]["data"]["position"] == 3
    assert client.get("/api/dashboard").json()["observations"]
    assert add_monitor(client, tracked) == monitor_id
    assert not client.app.state.worker.process_one()


def test_failed_check_is_not_a_ranking_drop(client, tracked):
    monitor_id = add_monitor(client, tracked)
    client.app.state.worker.process_one()
    FakeGateway.failures = [ProviderError("Upstream unavailable")]
    client.post(f"/api/monitors/{monitor_id}/check")
    client.app.state.worker.process_one()
    rows = client.get("/api/dashboard").json()["observations"]
    assert [row["status"] for row in rows] == ["success", "error"]
    assert rows[0]["data"]["position"] == 3 and "position" not in rows[1]["data"]


@pytest.mark.parametrize("filtered", [False, True])
def test_delayed_jobs_appear_on_the_day_they_were_checked(client, tracked, filtered):
    add_monitor(client, tracked)
    with client.app.state.service.db.session.begin() as session:
        session.scalar(select(Run)).created_at = now() - 3 * 86400
    assert client.app.state.worker.process_one()
    params = {"days": 1, **({"app_id": tracked} if filtered else {})}
    current = client.get("/api/dashboard", params=params).json()
    assert len(current["observations"]) == len(current["runs"]) == 1
    past = client.get("/api/dashboard", params={**params, "start": 0, "end": now() - 86400}).json()
    assert past["observations"] == past["runs"] == []


def test_transient_retry_then_success(client, tracked):
    add_monitor(client, tracked)
    FakeGateway.failures = [ProviderError("Try later", retryable=True)]
    client.app.state.worker.process_one()
    with client.app.state.service.db.session.begin() as session:
        run = session.scalar(select(Run))
        assert run.status == "queued" and run.attempts == 1 and run.available_at > now()
        run.available_at = 0
    client.app.state.worker.process_one()
    with client.app.state.service.db.session() as session:
        run = session.scalar(select(Run))
        assert run.status == "success" and run.attempts == 2


def test_pause_resume_and_overdue_dispatch(client, tracked):
    monitor_id = add_monitor(client, tracked)
    client.patch(f"/api/monitors/{monitor_id}", json={"enabled": False})
    assert not client.app.state.worker.process_one()
    client.patch(f"/api/monitors/{monitor_id}", json={"enabled": True})
    assert client.app.state.worker.process_one()
    with client.app.state.service.db.session.begin() as session:
        session.get(Monitor, monitor_id).next_run_at = now() - 86400 * 3
    client.app.state.service.dispatch()
    client.app.state.service.dispatch()
    assert client.app.state.worker.process_one()
    assert not client.app.state.worker.process_one()


def test_listing_refreshes_are_manual_and_never_rescheduled(client, tracked):
    with client.app.state.service.db.session.begin() as session:
        session.merge(Setting(key="preferences", value={"profile_frequency": "daily"}))
        for listing in session.scalars(select(Listing)):
            listing.next_refresh_at = 0
    calls = len(FakeGateway.calls)
    client.app.state.service.dispatch()
    assert not client.app.state.worker.process_one()
    assert len(FakeGateway.calls) == calls
    assert client.get("/api/state").json()["estimated_monthly"] == {"min": 0, "max": 0}
    response = client.post(f"/api/apps/{tracked}/refresh")
    assert response.status_code == 200
    assert len(response.json()["run_ids"]) == 2
    assert client.post(f"/api/apps/{tracked}/refresh").json() == response.json()
    assert client.app.state.worker.process_one()
    assert client.app.state.worker.process_one()
    profiles = client.get("/api/dashboard").json()["profiles"]
    assert len(profiles) == 4
    assert len(FakeGateway.calls) == calls + 2
    client.app.state.service.dispatch()
    assert not client.app.state.worker.process_one()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_restart_cancels_old_automatic_listing_jobs_but_keeps_manual(
    client, tracked, monkeypatch, status
):
    db, worker = client.app.state.service.db, client.app.state.worker
    with db.session.begin() as session:
        listings = list(session.scalars(select(Listing)))
        for index, listing in enumerate(listings):
            session.add(
                Run(
                    kind="profile",
                    listing_id=listing.id,
                    status=status,
                    params={
                        **{
                            key: getattr(listing, key)
                            for key in ("platform", "external_id", "country", "language")
                        },
                        **({"manual": True} if index else {}),
                    },
                )
            )
    monkeypatch.setattr(worker, "loop", lambda: None)
    worker.start()
    worker.stop()
    with db.session() as session:
        assert sorted(session.scalars(select(Run.status))) == ["cancelled", "queued"]
    calls = len(FakeGateway.calls)
    assert worker.process_one()
    assert not worker.process_one()
    assert len(FakeGateway.calls) == calls + 1


def test_bing_ignores_unsupported_locale_and_usage_shared(client, tracked):
    first = add_monitor(client, tracked, "bing_copilot", "best task apps", country="us")
    second = add_monitor(client, tracked, "bing_copilot", "best task apps", country="in")
    assert first == second
    state = client.get("/api/state").json()
    assert state["monitors"][0]["country"] == "global"
    assert state["estimated_monthly"]["min"] == 30


def test_reanalysis_and_csv_formula_escape(client, tracked):
    add_monitor(client, tracked, "google_ai_mode", "=HYPERLINK(test)")
    client.app.state.worker.process_one()
    assert client.post(f"/api/apps/{tracked}/reanalyze").json()["updated"] == 1
    rows = client.get("/api/dashboard").json()["observations"]
    assert rows[0]["retrospective"]
    assert "'=HYPERLINK(test)" in client.get("/api/export.csv").text


def test_backup_and_restart_retain_history(client, tracked, tmp_path):
    add_monitor(client, tracked)
    client.app.state.worker.process_one()
    backup = client.get("/api/backup")
    path = tmp_path / "download.sqlite3"
    path.write_bytes(backup.content)
    with sqlite3.connect(path) as connection:
        assert connection.execute("select count(*) from observations").fetchone()[0] == 1
        assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
    with TestClient(
        create_app(tmp_path, start_worker=False, gateway_factory=FakeGateway)
    ) as restarted:
        sign_in(restarted)
        assert restarted.get("/api/state").json()["apps"][0]["id"] == tracked
        assert restarted.get("/api/dashboard").json()["observations"][0]["data"]["position"] == 3


def test_monthly_schedule_uses_calendar_month():
    start = datetime(2024, 1, 31, tzinfo=UTC).timestamp()
    assert datetime.fromtimestamp(next_check(start, "monthly"), UTC) == datetime(
        2024, 2, 29, tzinfo=UTC
    )


def test_cross_site_requests_and_session_reuse_blocked(client):
    assert (
        client.post(
            "/api/check",
            headers={"Origin": "https://attacker.test", "Sec-Fetch-Site": "cross-site"},
        ).status_code
        == 403
    )
    assert client.get("/api/state", headers={"Host": "attacker.test"}).status_code == 401


def test_remote_authentication(tmp_path):
    with TestClient(
        create_app(tmp_path, start_worker=False), base_url="http://192.0.2.10:8080"
    ) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/state").status_code == 401
        assert client.get("/api/auth/status").json()["setup_required"]
        sign_in(client)
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/state", auth=("owner", "wrong")).status_code == 401
        assert client.get("/api/state", auth=("owner", TEST_PASSWORD)).status_code == 401
        sign_in(client)
        assert client.get("/api/state").status_code == 200
