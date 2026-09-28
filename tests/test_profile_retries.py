from types import SimpleNamespace

import pytest

from apptrail.db import now
from apptrail.engines import Gateway


@pytest.mark.parametrize("recovers", [True, False])
def test_profile_refresh_retries_incomplete_products_with_backoff(
    client, tracked, monkeypatch, recovers
):
    clock = {"now": now() + 1}
    monkeypatch.setattr("apptrail.worker.now", lambda: clock["now"])
    play_calls = []

    def search(**params):
        if params["engine"] == "apple_product":
            return {"id": "123456", "title": "Todo Example", "rating": 4.8}
        play_calls.append(params)
        if recovers and len(play_calls) > 2:
            return {"product_info": {"title": "Todo Example", "rating": 4.7}}
        return {"search_metadata": {"status": "Success"}}

    monkeypatch.setattr(Gateway, "client", lambda self, key: SimpleNamespace(search=search))
    client.app.state.service.gateway_factory = Gateway
    worker = client.app.state.worker
    profiles_url = f"/api/apps/{tracked}/profiles"
    before = len(client.get(profiles_url).json()["profiles"])
    response = client.post(f"/api/apps/{tracked}/refresh")
    assert response.status_code == 200
    run_ids = response.json()["run_ids"]
    assert worker.process_one()
    assert worker.process_one()
    runs = [client.get(f"/api/runs/{run_id}").json() for run_id in run_ids]
    apple = next(run for run in runs if run["params"]["platform"] == "ios")
    play = next(run for run in runs if run["params"]["platform"] == "android")
    assert apple["status"] == "success"
    assert play["status"] == "queued"
    assert play["attempts"] == 1 and play["requests_count"] == 2
    assert play["available_at"] > clock["now"]
    assert len(play["responses"]) == 2
    assert len(client.get(profiles_url).json()["profiles"]) == before + 1
    assert not worker.process_one()

    clock["now"] = play["available_at"]
    assert worker.process_one()
    play = client.get(f"/api/runs/{play['id']}").json()
    if recovers:
        assert play["status"] == "success"
        assert play["attempts"] == 2 and play["requests_count"] == 3
        assert play["result"]["profile"]["metadata_json"]["rating"] == 4.7
        assert len(client.get(profiles_url).json()["profiles"]) == before + 2
    else:
        assert play["status"] == "queued"
        assert play["attempts"] == 2 and play["available_at"] > clock["now"]
        assert not worker.process_one()
        clock["now"] = play["available_at"]
        assert worker.process_one()
        play = client.get(f"/api/runs/{play['id']}").json()
        assert play["status"] == "error"
        assert play["attempts"] == 3 and play["requests_count"] == 6
        assert len(client.get(profiles_url).json()["profiles"]) == before + 1
    assert len(play["responses"]) == play["requests_count"]
    assert not worker.process_one()


def test_profile_refresh_preserves_last_good_data_when_provider_returns_no_results(
    client, tracked, monkeypatch
):
    error = "Google Play hasn't returned any results for this query."

    def search(**params):
        if params["engine"] == "apple_product":
            return {"id": "123456", "title": "Todo Example", "rating": 4.8}
        return {
            "search_metadata": {"status": "Success", "id": "empty-product-search"},
            "search_parameters": params,
            "search_information": {"organic_results_state": "Fully empty"},
            "error": error,
        }

    monkeypatch.setattr(Gateway, "client", lambda self, key: SimpleNamespace(search=search))
    client.app.state.service.gateway_factory = Gateway
    profiles_url = f"/api/apps/{tracked}/profiles"
    before = client.get(profiles_url).json()["profiles"]
    before_play = [row for row in before if row["platform"] == "android"]
    before_listing = next(
        item
        for item in client.get("/api/state").json()["apps"][0]["listings"]
        if item["platform"] == "android"
    )
    response = client.post(f"/api/apps/{tracked}/refresh")
    assert response.status_code == 200
    worker = client.app.state.worker
    assert worker.process_one()
    assert worker.process_one()
    runs = [client.get(f"/api/runs/{run_id}").json() for run_id in response.json()["run_ids"]]
    play = next(run for run in runs if run["params"]["platform"] == "android")
    assert play["status"] == "error"
    assert play["attempts"] == 1 and play["requests_count"] == 2
    assert error in play["error"]
    assert play["result"] == {}
    assert [item["data"]["error"] for item in play["responses"]] == [error, error]
    after = client.get(profiles_url).json()["profiles"]
    assert [row for row in after if row["platform"] == "android"] == before_play
    after_listing = next(
        item
        for item in client.get("/api/state").json()["apps"][0]["listings"]
        if item["platform"] == "android"
    )
    assert after_listing["metadata_json"] == before_listing["metadata_json"]
    assert not worker.process_one()
