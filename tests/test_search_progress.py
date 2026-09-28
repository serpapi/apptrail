from conftest import add_monitor
from sqlalchemy import select

from apptrail.db import Run, now
from apptrail.engines import ProviderError


def test_progress_includes_the_entire_queue_and_current_run(client, tracked):
    for index in range(35):
        add_monitor(client, tracked, query=f"task {index}")
    with client.app.state.service.db.session.begin() as session:
        run = session.scalar(select(Run).order_by(Run.id.desc()))
        run.status = "running"
        run.result = {"must_not_be_in_progress": True}
        current_id = run.id
    state = client.get("/api/state").json()
    assert state["jobs"] == {"queued": 34, "running": 1}
    assert state["current_job"]["id"] == current_id
    assert state["current_job"]["params"]["query"] == "task 34"
    assert "result" not in state["current_job"]
    assert "responses" not in state["current_job"]


def test_progress_exposes_retry_time_then_clears_after_completion(client, tracked):
    from conftest import FakeGateway

    add_monitor(client, tracked)
    FakeGateway.failures.append(ProviderError("Temporary failure", retryable=True))
    worker = client.app.state.worker
    worker.process_one()
    state = client.get("/api/state").json()
    assert state["jobs"] == {"queued": 1}
    assert state["current_job"]["available_at"] > now()
    with client.app.state.service.db.session.begin() as session:
        session.scalar(select(Run)).available_at = 0
    worker.process_one()
    state = client.get("/api/state").json()
    assert state["jobs"] == {"success": 1}
    assert state["current_job"] is None


def test_progress_prioritizes_ready_checks_over_delayed_retries(client, tracked):
    add_monitor(client, tracked, query="retry later")
    with client.app.state.service.db.session.begin() as session:
        session.scalar(select(Run)).available_at = now() + 60
    add_monitor(client, tracked, query="ready now")
    assert client.get("/api/state").json()["current_job"]["params"]["query"] == "ready now"
