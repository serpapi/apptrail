import pytest
from conftest import FakeGateway, add_monitor
from sqlalchemy import select

from apptrail.db import App, Run, Target
from apptrail.engines import ProviderError


def test_removing_last_target_cancels_pending_search(client, tracked):
    monitor_id = add_monitor(client, tracked)
    calls = len(FakeGateway.calls)
    response = client.delete(f"/api/monitors/{monitor_id}/targets/{tracked}")
    assert response.status_code == 200
    assert not client.app.state.worker.process_one()
    assert len(FakeGateway.calls) == calls
    with client.app.state.service.db.session() as session:
        assert session.scalar(select(Run)).status == "cancelled"


def test_removing_one_target_keeps_shared_search(client, tracked):
    monitor_id = add_monitor(client, tracked, source="google_ai_mode")
    with client.app.state.service.db.session.begin() as session:
        competitor = App(name="Competitor")
        session.add(competitor)
        session.flush()
        session.add(Target(monitor_id=monitor_id, app_id=competitor.id))
    assert client.delete(f"/api/monitors/{monitor_id}/targets/{tracked}").status_code == 200
    assert client.app.state.worker.process_one()
    with client.app.state.service.db.session() as session:
        assert session.scalar(select(Run)).status == "success"


@pytest.mark.parametrize("action", ["pause", "archive", "detach"])
def test_cancel_during_failed_request_prevents_retries(client, tracked, monkeypatch, action):
    monitor_id = add_monitor(client, tracked)

    def failing_search(gateway, spec):
        gateway.requests_count = 1
        if action == "pause":
            response = client.patch(f"/api/monitors/{monitor_id}", json={"enabled": False})
        elif action == "archive":
            response = client.patch(
                f"/api/apps/{tracked}", json={"name": "Todo Example", "archived": True}
            )
        else:
            response = client.delete(f"/api/monitors/{monitor_id}/targets/{tracked}")
        assert response.status_code == 200
        raise ProviderError("Temporary provider failure", retryable=True)

    monkeypatch.setattr(FakeGateway, "search", failing_search)
    assert client.app.state.worker.process_one()
    with client.app.state.service.db.session() as session:
        run = session.scalar(select(Run))
        assert run.status == "cancelled"
        assert run.attempts == run.requests_count == 1
        assert run.finished_at is not None
    assert not client.app.state.worker.process_one()


def test_archive_during_listing_request_prevents_retries(client, tracked, monkeypatch):
    client.post(f"/api/apps/{tracked}/refresh")

    def failing_product(gateway, *args):
        gateway.requests_count = 1
        assert (
            client.patch(
                f"/api/apps/{tracked}", json={"name": "Todo Example", "archived": True}
            ).status_code
            == 200
        )
        raise ProviderError("Temporary provider failure", retryable=True)

    monkeypatch.setattr(FakeGateway, "product", failing_product)
    assert client.app.state.worker.process_one()
    assert not client.app.state.worker.process_one()
    with client.app.state.service.db.session() as session:
        assert set(session.scalars(select(Run.status))) == {"cancelled"}


def test_manual_check_of_paused_monitor_can_retry(client, tracked):
    monitor_id = add_monitor(client, tracked)
    client.patch(f"/api/monitors/{monitor_id}", json={"enabled": False})
    run_id = client.post(f"/api/monitors/{monitor_id}/check").json()["run_id"]
    FakeGateway.failures = [ProviderError("Temporary provider failure", retryable=True)]
    assert client.app.state.worker.process_one()
    with client.app.state.service.db.session.begin() as session:
        run = session.get(Run, run_id)
        assert run.status == "queued"
        run.available_at = 0
    assert client.app.state.worker.process_one()
    with client.app.state.service.db.session() as session:
        assert session.get(Run, run_id).status == "success"


def test_restart_does_not_resume_cancelled_inflight_search(client, tracked, monkeypatch):
    monitor_id = add_monitor(client, tracked)
    db, worker = client.app.state.service.db, client.app.state.worker
    with db.session.begin() as session:
        session.scalar(select(Run)).status = "running"
    client.patch(f"/api/monitors/{monitor_id}", json={"enabled": False})
    monkeypatch.setattr(worker, "loop", lambda: None)
    worker.start()
    worker.stop()
    assert not worker.process_one()
    with db.session() as session:
        assert session.scalar(select(Run)).status == "cancelled"


@pytest.mark.parametrize("action", ["resume", "check", "replace"])
def test_explicit_restart_of_inflight_check_restores_retries(client, tracked, monkeypatch, action):
    monitor_id = add_monitor(client, tracked)
    original_search = FakeGateway.search

    def failing_search(gateway, spec):
        gateway.requests_count = 1
        if action == "replace":
            replacement = client.post(
                f"/api/monitors/{monitor_id}/replace", json={"query": "replacement"}
            )
            response = client.post(
                f"/api/monitors/{replacement.json()['id']}/replace", json={"query": "task manager"}
            )
        else:
            client.patch(f"/api/monitors/{monitor_id}", json={"enabled": False})
            response = (
                client.patch(f"/api/monitors/{monitor_id}", json={"enabled": True})
                if action == "resume"
                else client.post(f"/api/monitors/{monitor_id}/check")
            )
        assert response.status_code == 200
        raise ProviderError("Temporary provider failure", retryable=True)

    monkeypatch.setattr(FakeGateway, "search", failing_search)
    assert client.app.state.worker.process_one()
    with client.app.state.service.db.session.begin() as session:
        run = session.scalar(select(Run).where(Run.monitor_id == monitor_id))
        assert run.status == "queued" and not run.params.get("cancel_requested")
        run.available_at = 0
    monkeypatch.setattr(FakeGateway, "search", original_search)
    assert client.app.state.worker.process_one()
    with client.app.state.service.db.session() as session:
        assert session.scalar(select(Run).where(Run.monitor_id == monitor_id)).status == "success"
