from conftest import FakeGateway, add_monitor


def test_archiving_excludes_app_from_workspace_metrics_but_keeps_saved_history(
    client, tracked, monkeypatch
):
    def discover_competitor(self, platform, query, country, language):
        return [self.product(platform, "654321", country, language)]

    monkeypatch.setattr(FakeGateway, "discover", discover_competitor)
    discovery = client.post("/api/discover", json={"platform": "ios", "query": "Competitor"})
    assert discovery.status_code == 200
    created = client.post(
        "/api/apps",
        json={
            "name": "Competitor",
            "candidate_tokens": [discovery.json()["candidates"][0]["token"]],
        },
    )
    assert created.status_code == 201
    rival = created.json()["id"]
    monitor = add_monitor(client, tracked)
    assert (
        client.post(f"/api/monitors/{monitor}/targets", json={"app_ids": [rival]}).status_code
        == 200
    )
    assert client.app.state.worker.process_one()

    before = client.get("/api/dashboard").json()["observations"]
    assert {row["app_id"] for row in before} == {tracked, rival}
    distribution = client.get("/api/ranking-distribution").json()
    assert distribution["total"] == 2
    assert distribution["counts"]["top3"] == distribution["counts"]["not_found"] == 1

    assert (
        client.patch(
            f"/api/apps/{rival}", json={"name": "Competitor", "archived": True}
        ).status_code
        == 200
    )
    for params in ({}, {"country": "us", "source": "apple_app_store"}):
        dashboard = client.get("/api/dashboard", params=params).json()
        assert {row["app_id"] for row in dashboard["observations"]} == {tracked}
        distribution = client.get("/api/ranking-distribution", params=params).json()
        assert distribution["total"] == 1
        assert distribution["counts"]["top3"] == 1
        assert distribution["counts"]["not_found"] == 0

    retained = client.get("/api/dashboard", params={"app_id": rival}).json()["observations"]
    assert len(retained) == 1 and retained[0]["app_id"] == rival
    assert client.get("/api/ranking-distribution", params={"app_id": rival}).json()["total"] == 1
    exported = list(client.app.state.service.export_observations())
    assert {(row["app_id"], row["app_name"]) for row in exported} == {
        (tracked, "Todo Example"),
        (rival, "Competitor"),
    }
    evidence = client.get(f"/api/runs/{retained[0]['run_id']}").json()
    assert {row["app_id"] for row in evidence["observations"]} == {tracked, rival}

    assert (
        client.patch(
            f"/api/apps/{rival}", json={"name": "Competitor", "archived": False}
        ).status_code
        == 200
    )
    assert {row["app_id"] for row in client.get("/api/dashboard").json()["observations"]} == {
        tracked,
        rival,
    }
    assert client.get("/api/ranking-distribution").json()["total"] == 2


def test_readding_paused_query_reports_saved_state_without_scheduling_a_check(client, tracked):
    spec = {"source": "apple_app_store", "query": "task manager", "app_ids": [tracked]}
    first = client.post("/api/monitors", json=spec)
    assert first.status_code == 201 and first.json()["enabled"] is True
    monitor = first.json()["id"]
    assert client.app.state.worker.process_one()
    assert (
        client.patch(
            f"/api/monitors/{monitor}", json={"enabled": False, "frequency": "weekly"}
        ).status_code
        == 200
    )
    calls = list(FakeGateway.calls)
    response = client.post("/api/monitors/batch", json={"monitors": [spec]}).json()
    reused = response["monitors"][0]
    assert response["created"] == 0 and reused["id"] == monitor
    assert reused["enabled"] is False and reused["frequency"] == "weekly"
    assert not client.app.state.worker.process_one()
    assert FakeGateway.calls == calls
