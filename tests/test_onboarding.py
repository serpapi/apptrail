from conftest import FakeGateway, add_monitor, sign_in
from fastapi.testclient import TestClient

from apptrail.api import create_app


def progress(client):
    return client.get("/api/state").json()["onboarding"]


def test_checklist_follows_existing_apps_and_queries(client, tracked):
    assert progress(client)["completed"] == ["app"]
    store = add_monitor(client, tracked)
    assert progress(client)["completed"] == ["app", "store"]
    assert progress(client)["visible"]
    add_monitor(client, tracked, "google_ai_mode", "Best task apps?")
    assert progress(client)["completed"] == ["app", "store", "ai"]
    assert not progress(client)["visible"]
    client.patch(f"/api/monitors/{store}", json={"enabled": False})
    assert not progress(client)["visible"]


def test_skip_dismiss_and_restore_persist_across_runs(client, tracked, tmp_path):
    response = client.post("/api/onboarding", json={"action": "skip", "step": "store"})
    assert response.status_code == 200
    assert response.json()["skipped"] == ["store"]
    assert response.json()["visible"]
    with TestClient(create_app(tmp_path, start_worker=False, gateway_factory=FakeGateway)) as run:
        sign_in(run)
        assert progress(run)["skipped"] == ["store"]
        assert run.post("/api/onboarding", json={"action": "dismiss"}).status_code == 200
    with TestClient(create_app(tmp_path, start_worker=False, gateway_factory=FakeGateway)) as run:
        sign_in(run)
        assert progress(run)["dismissed"] and not progress(run)["visible"]
        restored = run.post("/api/onboarding", json={"action": "restore"}).json()
        assert restored == {
            "completed": ["app"],
            "skipped": [],
            "dismissed": False,
            "visible": True,
        }


def test_all_steps_optional_and_skipping_does_not_run_searches(client):
    assert progress(client)["completed"] == []
    before = list(FakeGateway.calls)
    for step in ["app", "store", "ai"]:
        assert (
            client.post("/api/onboarding", json={"action": "skip", "step": step}).status_code == 200
        )
    assert not progress(client)["visible"]
    assert progress(client)["completed"] == []
    assert FakeGateway.calls == before
    assert client.get("/api/dashboard").json()["runs"] == []


def test_skipped_step_can_be_completed_later(client, tracked):
    client.post("/api/onboarding", json={"action": "skip", "step": "store"})
    add_monitor(client, tracked)
    assert progress(client)["completed"] == ["app", "store"]
    client.post("/api/onboarding", json={"action": "skip", "step": "ai"})
    assert not progress(client)["visible"]


def test_checklist_rejects_invalid_and_untrusted_changes(client):
    for body in [{"action": "skip"}, {"action": "skip", "step": "unknown"}, {"action": "delete"}]:
        assert client.post("/api/onboarding", json=body).status_code == 422
    assert (
        client.post(
            "/api/onboarding", json={"action": "dismiss"}, headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    assert progress(client)["visible"]
    client.cookies.clear()
    assert client.post("/api/onboarding", json={"action": "dismiss"}).status_code == 401


def test_listing_history_introduction_dismissal_persists(client, tmp_path):
    assert client.get("/api/state").json()["dismissed_hints"] == []
    assert not client.get("/api/state").json()["listing_history_started"]
    assert client.post("/api/hints/listing-history/dismiss").status_code == 200
    with TestClient(
        create_app(tmp_path, start_worker=False, gateway_factory=FakeGateway)
    ) as reopened:
        sign_in(reopened)
        assert reopened.get("/api/state").json()["dismissed_hints"] == ["listing-history"]
