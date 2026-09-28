import pytest
from conftest import FakeGateway, add_monitor, sign_in
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apptrail.api import create_app
from apptrail.db import App, Competitor, Listing, Monitor, Observation, Run, Target, now


def competitor(client, name="Competitor", platform="ios", identifier="999"):
    with client.app.state.service.db.session.begin() as session:
        app = App(name=name)
        session.add(app)
        session.flush()
        session.add(
            Listing(
                app_id=app.id,
                platform=platform,
                external_id=identifier,
                title=name,
                url=f"https://example.com/apps/{identifier}",
            )
        )
        return app.id


@pytest.mark.parametrize(
    "source", ["apple_app_store", "google_ai_mode", "google_ai_overview", "bing_copilot"]
)
def test_add_competitor_reuses_history_and_future_checks(client, tracked, source):
    rival = competitor(client, name="Todo Example competitor")
    monitor_id = add_monitor(client, tracked, source, frequency="weekly")
    worker, db = client.app.state.worker, client.app.state.service.db
    assert worker.process_one()
    assert worker.process_one() is False
    before_calls = list(FakeGateway.calls)
    before_state = client.get("/api/state").json()
    monitor = before_state["monitors"][0]
    response = client.post(f"/api/monitors/{monitor_id}/targets", json={"app_ids": [rival, rival]})
    assert response.status_code == 200, response.text
    assert response.json() == {"added_app_ids": [rival], "saved_checks": 1}
    assert FakeGateway.calls == before_calls
    assert not worker.process_one()
    after = client.get("/api/state").json()
    assert after["estimated_monthly"] == before_state["estimated_monthly"]
    for key in ["frequency", "next_run_at", "enabled", "signature"]:
        assert after["monitors"][0][key] == monitor[key]
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 1
    details = client.get(f"/api/monitors/{monitor_id}/targets").json()
    assert {t["app_id"] for t in details["targets"]} == {tracked, rival}
    rival_data = next(t for t in details["targets"] if t["app_id"] == rival)
    assert rival_data["retrospective"] and rival_data["data"]["found"] is False
    assert client.post(f"/api/monitors/{monitor_id}/targets", json={"app_ids": [rival]}).json() == {
        "added_app_ids": [],
        "saved_checks": 0,
    }
    client.post(f"/api/monitors/{monitor_id}/check")
    assert worker.process_one()
    assert not worker.process_one()
    assert len([call for call in FakeGateway.calls if call[0] == "search"]) == 2
    details = client.get(f"/api/monitors/{monitor_id}/targets").json()
    assert len(details["targets"]) == 2
    assert all(not t["retrospective"] for t in details["targets"])


def test_store_backfill_matches_identifier_not_similar_name(client, tracked):
    rival = competitor(client, name="Completely different display name")
    monitor_id = add_monitor(client, tracked)
    assert client.app.state.worker.process_one()
    with client.app.state.service.db.session.begin() as session:
        run = session.scalar(select(Run))
        result = dict(run.result)
        result["items"] = [
            *result["items"],
            {
                "platform": "ios",
                "external_id": "999",
                "title": "A store title",
                "position": 7,
                "primary": True,
                "section": "Search results",
                "url": "https://apps.apple.com/us/app/id999",
            },
        ]
        run.result = result
    client.post(f"/api/monitors/{monitor_id}/targets", json={"app_ids": [rival]})
    data = client.get(f"/api/monitors/{monitor_id}/targets").json()
    match = next(t["data"] for t in data["targets"] if t["app_id"] == rival)
    assert match["position"] == 7 and match["evidence"][0]["type"] == "identifier"


def test_paused_search_and_historical_results_keep_their_dates(client, tracked):
    rival = competitor(client)
    monitor_id = add_monitor(client, tracked)
    assert client.app.state.worker.process_one()
    client.patch(f"/api/monitors/{monitor_id}", json={"enabled": False})
    with client.app.state.service.db.session.begin() as session:
        run = session.scalar(select(Run))
        run.created_at = run.started_at = run.finished_at = now() - 90 * 86400
        old_time = run.started_at
        due = session.get(Monitor, monitor_id).next_run_at
    response = client.post(f"/api/monitors/{monitor_id}/targets", json={"app_ids": [rival]})
    assert response.status_code == 200
    details = client.get(f"/api/monitors/{monitor_id}/targets").json()
    assert details["checked_at"] == old_time
    assert details["monitor"]["enabled"] is False
    assert details["monitor"]["next_run_at"] == due
    assert not client.app.state.worker.process_one()
    assert len(client.get(f"/api/dashboard?app_id={rival}&days=120").json()["observations"]) == 1


def test_pending_search_adds_competitor_without_duplicate_job(client, tracked):
    rival = competitor(client)
    monitor_id = add_monitor(client, tracked)
    result = client.post(f"/api/monitors/{monitor_id}/targets", json={"app_ids": [rival]}).json()
    assert result["saved_checks"] == 0
    details = client.get(f"/api/monitors/{monitor_id}/targets").json()
    assert details["run_id"] is None and all(t["data"] is None for t in details["targets"])
    assert client.app.state.worker.process_one()
    assert not client.app.state.worker.process_one()
    details = client.get(f"/api/monitors/{monitor_id}/targets").json()
    assert all(t["data"] is not None for t in details["targets"])


def test_rejects_incompatible_archived_and_unknown_apps_atomically(client, tracked):
    rival = competitor(client)
    android = competitor(client, name="Android only", platform="android", identifier="org.rival")
    archived = competitor(client, name="Archived", identifier="888")
    monitor_id = add_monitor(client, tracked)
    with client.app.state.service.db.session.begin() as session:
        session.get(App, archived).archived = True
    for invalid in [android, archived, 99999]:
        response = client.post(
            f"/api/monitors/{monitor_id}/targets", json={"app_ids": [rival, invalid]}
        )
        assert response.status_code == 422, response.text
    assert (
        client.post(f"/api/monitors/{monitor_id}/targets", json={"app_ids": []}).status_code == 422
    )
    assert client.post("/api/monitors/99999/targets", json={"app_ids": [rival]}).status_code == 404
    assert client.get("/api/monitors/99999/targets").status_code == 404
    with client.app.state.service.db.session() as session:
        assert list(
            session.scalars(select(Target.app_id).where(Target.monitor_id == monitor_id))
        ) == [tracked]


def test_google_play_accepts_android_rival_and_ai_accepts_either_platform(client, tracked):
    rival = competitor(client, platform="android", identifier="org.rival")
    for source in ["google_play", "bing_copilot"]:
        monitor_id = add_monitor(client, tracked, source)
        response = client.post(f"/api/monitors/{monitor_id}/targets", json={"app_ids": [rival]})
        assert response.status_code == 200


def test_comparison_uses_last_successful_check_after_failure(client, tracked):
    from apptrail.engines import ProviderError

    rival = competitor(client)
    monitor_id = add_monitor(client, tracked)
    assert client.app.state.worker.process_one()
    original = client.get(f"/api/monitors/{monitor_id}/targets").json()
    FakeGateway.failures = [ProviderError("Unavailable")]
    client.post(f"/api/monitors/{monitor_id}/check")
    assert client.app.state.worker.process_one()
    client.post(f"/api/monitors/{monitor_id}/targets", json={"app_ids": [rival]})
    details = client.get(f"/api/monitors/{monitor_id}/targets").json()
    assert details["run_id"] == original["run_id"]
    assert details["checked_at"] == original["checked_at"]
    with client.app.state.service.db.session() as session:
        assert (
            len(list(session.scalars(select(Observation).where(Observation.app_id == rival)))) == 1
        )


def test_bulk_competitor_preserves_selected_markets_history_and_schedules(
    client, tracked, tmp_path
):
    rival = competitor(client)
    with client.app.state.service.db.session.begin() as session:
        session.add(
            Listing(
                app_id=rival,
                platform="android",
                external_id="org.rival",
                title="Rival",
                url="https://example.com/rival",
            )
        )
    monitors = {}
    for source in ("apple_app_store", "google_play", "google_ai_mode"):
        for country in ("us", "gb"):
            monitors[source, country] = add_monitor(
                client, tracked, source, country=country, frequency="weekly"
            )
    global_id = add_monitor(client, tracked, "bing_copilot")
    while client.app.state.worker.process_one():
        pass
    chosen = [
        monitors["apple_app_store", "us"],
        monitors["google_play", "gb"],
        monitors["google_ai_mode", "gb"],
        global_id,
    ]
    client.patch(f"/api/monitors/{chosen[0]}", json={"enabled": False})
    before = client.get("/api/state").json()
    calls = list(FakeGateway.calls)
    response = client.post(
        f"/api/apps/{tracked}/competitors",
        json={"existing_app_id": rival, "monitor_ids": [*chosen, chosen[0]]},
    )
    assert response.status_code == 201, response.text
    assert response.json()["monitor_ids"] == chosen
    after = client.get("/api/state").json()
    assert after["estimated_monthly"] == before["estimated_monthly"]
    for old, new in zip(before["monitors"], after["monitors"], strict=True):
        assert (rival in new["app_ids"]) == (new["id"] in chosen)
        assert {key: value for key, value in old.items() if key != "app_ids"} == {
            key: value for key, value in new.items() if key != "app_ids"
        }
    assert {
        row["monitor_id"] for row in after["latest_observations"] if row["app_id"] == rival
    } == set(chosen)
    assert all(
        row["retrospective"] for row in after["latest_observations"] if row["app_id"] == rival
    )
    assert not client.app.state.worker.process_one()
    assert FakeGateway.calls == calls
    assert (
        client.post(
            f"/api/apps/{tracked}/competitors",
            json={"existing_app_id": rival, "monitor_ids": chosen},
        ).status_code
        == 201
    )
    with TestClient(
        create_app(tmp_path, start_worker=False, gateway_factory=FakeGateway)
    ) as reopened:
        sign_in(reopened)
        saved = next(app for app in reopened.get("/api/state").json()["apps"] if app["id"] == rival)
        assert saved["competitor_for"] == [tracked]


def test_bulk_competitor_rejects_invalid_selection_without_partial_links(client, tracked):
    rival = competitor(client)
    another = competitor(client, name="Other original", identifier="777")
    valid = add_monitor(client, tracked)
    android = add_monitor(client, tracked, "google_play")
    unrelated = add_monitor(client, another, query="different app")
    for original, target, chosen in [
        (tracked, rival, [valid, unrelated]),
        (tracked, rival, [valid, android]),
        (tracked, rival, [valid, 999999]),
        (tracked, tracked, [valid]),
        (999999, rival, [valid]),
        (tracked, 999999, [valid]),
    ]:
        result = client.post(
            f"/api/apps/{original}/competitors",
            json={"existing_app_id": target, "monitor_ids": chosen},
        )
        assert result.status_code == 422, result.text
    client.patch(f"/api/apps/{rival}", json={"name": "Rival", "archived": True})
    assert (
        client.post(
            f"/api/apps/{tracked}/competitors",
            json={"existing_app_id": rival, "monitor_ids": [valid]},
        ).status_code
        == 422
    )
    with client.app.state.service.db.session() as session:
        assert session.scalar(select(Competitor)) is None
        assert session.get(Target, (valid, rival)) is None


def test_create_competitor_with_both_stores_and_selected_queries(client, tracked, monkeypatch):
    def discover(self, platform, query, country, language):
        return [
            self.product(platform, "999" if platform == "ios" else "org.rival", country, language)
        ]

    monkeypatch.setattr(FakeGateway, "discover", discover)
    tokens = [
        client.post("/api/discover", json={"platform": platform, "query": "Rival"}).json()[
            "candidates"
        ][0]["token"]
        for platform in ("ios", "android")
    ]
    chosen = [
        add_monitor(client, tracked, source)
        for source in ("apple_app_store", "google_play", "google_ai_mode")
    ]
    app = {"name": "Rival", "candidate_tokens": tokens, "aliases": ["Rival app"]}
    calls = list(FakeGateway.calls)
    invalid = client.post(
        f"/api/apps/{tracked}/competitors", json={"app": app, "monitor_ids": [*chosen, 999999]}
    )
    assert invalid.status_code == 422
    assert FakeGateway.calls == calls
    assert len(client.get("/api/state").json()["apps"]) == 1
    response = client.post(
        f"/api/apps/{tracked}/competitors", json={"app": app, "monitor_ids": chosen}
    )
    assert response.status_code == 201, response.text
    state = client.get("/api/state").json()
    rival = next(item for item in state["apps"] if item["id"] == response.json()["id"])
    assert rival["competitor_for"] == [tracked]
    assert {listing["platform"] for listing in rival["listings"]} == {"ios", "android"}
    assert all(rival["id"] in monitor["app_ids"] for monitor in state["monitors"])


def test_competitor_can_belong_to_multiple_original_apps_and_start_without_queries(client, tracked):
    rival = competitor(client)
    another = competitor(client, name="Other original", identifier="777")
    for original in (tracked, another):
        assert (
            client.post(
                f"/api/apps/{original}/competitors", json={"existing_app_id": rival}
            ).status_code
            == 201
        )
    saved = next(app for app in client.get("/api/state").json()["apps"] if app["id"] == rival)
    assert set(saved["competitor_for"]) == {tracked, another}


def test_competitor_and_hint_writes_require_csrf_and_validate_payload(client, tracked):
    rival = competitor(client)
    url = f"/api/apps/{tracked}/competitors"
    for body in (
        {},
        {"existing_app_id": rival, "app": {"name": "Rival", "candidate_tokens": ["x"]}},
        {"existing_app_id": rival, "monitor_ids": [1] * 2001},
    ):
        assert client.post(url, json=body).status_code == 422
    assert (
        client.post(
            url, json={"existing_app_id": rival}, headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/hints/listing-history/dismiss", headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    assert client.post("/api/hints/unknown/dismiss").status_code == 422
