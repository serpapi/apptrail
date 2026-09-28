import os

import pytest

pytestmark = pytest.mark.live


@pytest.mark.parametrize(
    "source", ["apple_app_store", "google_play", "google_ai_mode", "google_ai_overview"]
)
def test_live_countries_remain_separate(live_client, source):
    client = live_client
    app_id = client.get("/api/state").json()["apps"][0]["id"]
    countries = ["us", "gb", "in"] if source in {"apple_app_store", "google_play"} else ["us", "gb"]
    specs = [
        {
            "query": "Todoist"
            if source in {"apple_app_store", "google_play"}
            else "best to do list apps for iPhone",
            "source": source,
            "country": country,
            "app_ids": [app_id],
        }
        for country in countries
    ]
    response = client.post("/api/monitors/batch", json={"monitors": specs})
    assert response.status_code == 201, response.text
    assert response.json()["created"] == len(countries)
    for country in countries:
        assert client.app.state.worker.process_one()
        rows = client.get(f"/api/dashboard?country={country}&source={source}").json()[
            "observations"
        ]
        assert len(rows) == 1
        run = client.get(f"/api/runs/{rows[0]['run_id']}").json()
        assert run["status"] == "success", run["error"]
        raw = run["responses"][0]
        key = "country" if source == "apple_app_store" else "gl"
        assert raw["params"][key] == country
        assert raw["data"]["search_parameters"][key] == country
        if source in {"apple_app_store", "google_play"}:
            assert run["result"]["items"] and rows[0]["data"]["found"]
        elif source == "google_ai_mode":
            assert run["result"]["text"] and run["result"]["answer_available"]
        secret = os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY")
        assert not (secret and secret in str(run)), "Credential appeared in run response"
    repeated = client.post("/api/monitors/batch", json={"monitors": specs}).json()
    assert repeated["created"] == 0
    assert len(client.get(f"/api/dashboard?source={source}").json()["runs"]) == len(countries)


def test_live_copilot_is_global_once(live_client):
    client = live_client
    app_id = client.get("/api/state").json()["apps"][0]["id"]
    specs = [
        {
            "source": "bing_copilot",
            "query": "best to do list apps for iPhone",
            "country": c,
            "app_ids": [app_id],
        }
        for c in ["us", "gb", "in"]
    ]
    result = client.post("/api/monitors/batch", json={"monitors": specs}).json()
    assert result["created"] == 1
    assert client.app.state.worker.process_one()
    data = client.get("/api/dashboard?country=global").json()
    assert len(data["observations"]) == 1
    assert data["runs"][0]["status"] == "success", data["runs"][0]["error"]
    assert data["observations"][0]["data"]["answer_available"]
    assert not client.get("/api/dashboard?country=us&source=bing_copilot").json()["observations"]
