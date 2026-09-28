"""Live public-interface tests. Set SERPAPI_API_KEY and run pytest -m live -v."""

import os
import time

import pytest
from conftest import add_monitor

from apptrail.db import Monitor, now

pytestmark = pytest.mark.live


def test_live_account(live_client):
    response = live_client.post("/api/account/refresh")
    assert response.status_code == 200, response.text
    assert response.json()["account_status"] == "Active"
    assert response.json()["total_searches_left"] is not None
    assert "api_key" not in response.json()


def test_live_onboarding_verifies_both_store_listings(live_client):
    state = live_client.get("/api/state").json()
    listings = state["apps"][0]["listings"]
    assert {(item["platform"], item["external_id"]) for item in listings} == {
        ("ios", "572688855"),
        ("android", "com.todoist"),
    }
    assert all(item["metadata_json"]["rating"] > 0 for item in listings)


@pytest.mark.parametrize(
    "source,query",
    [
        ("apple_app_store", "to do list"),
        ("google_play", "Todoist"),
        ("google_ai_mode", "What are the best to do list apps for iPhone?"),
        ("google_ai_overview", "best to do list apps for iPhone"),
        ("bing_copilot", "What are the best to do list apps for iPhone?"),
    ],
)
def test_live_tracking_sources(live_client, source, query):
    app_id = live_client.get("/api/state").json()["apps"][0]["id"]
    response = live_client.post(
        "/api/monitors", json={"source": source, "query": query, "app_ids": [app_id]}
    )
    assert response.status_code == 201, response.text
    monitor_id = response.json()["id"]
    run_id = live_client.post(f"/api/monitors/{monitor_id}/check").json()["run_id"]
    assert live_client.app.state.worker.process_one()
    run = live_client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "success", run["error"]
    assert run["responses"] and run["observations"]
    result = run["result"]
    if result["kind"] == "store":
        assert result["items"] and result["results_checked"] > 0
        assert run["observations"][0]["data"]["found"]
    else:
        # A Google overview can legitimately be absent for a particular live query.
        if source != "google_ai_overview" or result["answer_available"]:
            assert result["text"] and result["references"]
            assert run["observations"][0]["data"]["mentioned"]
    secret = os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY")
    leaked = bool(secret and secret in str(run))
    assert not leaked, "Credential appeared in the public run response"


def test_live_duplicate_and_refresh(live_client):
    state = live_client.get("/api/state").json()
    monitor_id = add_monitor(live_client, state["apps"][0]["id"], query="Todoist")
    with live_client.app.state.service.db.session.begin() as session:
        session.get(Monitor, monitor_id).next_run_at = now() - 86400 * 3
    live_client.app.state.service.dispatch()
    first = live_client.post(f"/api/monitors/{monitor_id}/check").json()
    assert live_client.post(f"/api/monitors/{monitor_id}/check").json() == first
    assert live_client.app.state.worker.process_one()
    assert live_client.get(f"/api/runs/{first['run_id']}").json()["status"] == "success"
    assert live_client.post(f"/api/apps/{state['apps'][0]['id']}/reanalyze").json()["updated"] == 1
    assert live_client.get("/api/export.csv").status_code == 200
    backup = live_client.get("/api/backup")
    assert backup.content.startswith(b"SQLite format 3")


def test_live_url_lookup_and_invalid_id(live_client):
    response = live_client.post(
        "/api/discover",
        json={"platform": "ios", "query": "https://apps.apple.com/us/app/todoist/id572688855"},
    )
    assert response.status_code == 200
    assert response.json()["candidates"][0]["external_id"] == "572688855"
    response = live_client.post(
        "/api/discover",
        json={
            "platform": "android",
            "query": "https://play.google.com/store/apps/details?id=com.apptrail.nonexistent.testing",
        },
    )
    assert response.status_code == 502
    assert response.json()["detail"]


def test_live_deeper_store_searches(live_client):
    for source, query, depth in [
        ("apple_app_store", "to do list", 2),
        ("google_play", "car racing", 2),
    ]:
        gateway = live_client.app.state.service.gateway()
        outcome = gateway.search(
            {
                "source": source,
                "query": query,
                "country": "us",
                "language": "en",
                "device": "",
                "depth": depth,
            }
        )
        assert outcome.result["results_checked"] > 0
        assert outcome.result["requested_depth"] == 2
        if source == "apple_app_store":
            assert outcome.responses[0]["params"]["num"] == 100
        if source == "google_play" and outcome.responses[0]["data"].get(
            "serpapi_pagination", {}
        ).get("next_page_token"):
            assert outcome.result["pages"] == 2


def test_live_listing_statistics_refresh(live_client):
    app_id = live_client.get("/api/state").json()["apps"][0]["id"]
    before = len(live_client.get("/api/dashboard").json()["profiles"])
    response = live_client.post(f"/api/apps/{app_id}/refresh")
    assert response.status_code == 200
    pending = set(response.json()["run_ids"])
    deadline = time.monotonic() + 180
    while pending:
        processed = live_client.app.state.worker.process_one()
        for run_id in pending.copy():
            run = live_client.get(f"/api/runs/{run_id}").json()
            assert run["status"] in {"queued", "running", "success"}, {
                "error": run["error"],
                "searches": [
                    {
                        "search_id": item["data"].get("search_metadata", {}).get("id"),
                        "status": item["data"].get("search_metadata", {}).get("status"),
                        "fields": sorted(item["data"]),
                    }
                    for item in run["responses"]
                ],
            }
            if run["status"] == "success":
                pending.remove(run_id)
        assert not pending or time.monotonic() < deadline, f"Refresh timed out: {sorted(pending)}"
        if pending and not processed:
            time.sleep(0.5)
    assert len(live_client.get("/api/dashboard").json()["profiles"]) == before + 2
