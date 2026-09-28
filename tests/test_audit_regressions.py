import pytest
from conftest import add_monitor
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from apptrail.db import App, Monitor, Run, Target, now


@pytest.mark.parametrize("path", ["/api/state", "/api/dashboard", "/api/export.csv"])
def test_summary_requests_do_not_load_raw_search_payloads(client, tracked, path):
    add_monitor(client, tracked)
    client.app.state.worker.process_one()
    db = client.app.state.service.db
    with db.session.begin() as session:
        run = session.scalar(select(Run))
        run.responses = [{"large_response": "x" * 500_000}]
        run_id = run.id
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        response = client.get(path)
        assert response.status_code == 200
        assert not any(
            "runs.responses" in sql or "runs.result" in sql or "run_payloads" in sql
            for sql in statements
        )
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)
    evidence = client.get(f"/api/runs/{run_id}").json()
    assert evidence["responses"][0]["large_response"] == "x" * 500_000
    assert evidence["result"]["kind"] == "store"


def test_statistics_endpoint_does_not_read_search_history(client, tracked):
    add_monitor(client, tracked)
    client.app.state.worker.process_one()
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = client.app.state.service.db.engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = client.get(f"/api/apps/{tracked}/profiles")
        assert response.status_code == 200
        assert len(response.json()["profiles"]) == 2
        assert not any("FROM runs" in sql or "JOIN runs" in sql for sql in statements)
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_csv_download_does_not_hold_a_database_connection(client, tracked):
    add_monitor(client, tracked)
    client.app.state.worker.process_one()
    held_connections = []

    async def watch_download(scope, receive, send):
        async def observe(message):
            if scope.get("path") == "/api/export.csv" and message["type"] == "http.response.body":
                held_connections.append(client.app.state.service.db.engine.pool.checkedout())
            await send(message)

        await client.app(scope, receive, observe)

    with TestClient(
        watch_download, cookies=client.cookies, headers=dict(client.headers)
    ) as download:
        response = download.get("/api/export.csv")
        assert response.status_code == 200 and "task manager" in response.text
    assert held_connections and not any(held_connections)


def test_latest_observation_is_available_outside_chart_date_range(client, tracked):
    monitor_id = add_monitor(client, tracked, frequency="monthly")
    client.app.state.worker.process_one()
    with client.app.state.service.db.session.begin() as session:
        run = session.scalar(select(Run))
        run.created_at = run.started_at = run.finished_at = now() - 31 * 86400
    assert client.get("/api/dashboard?days=7").json()["observations"] == []
    latest = client.get("/api/state").json()["latest_observations"]
    assert len(latest) == 1
    assert latest[0]["monitor_id"] == monitor_id and latest[0]["data"]["position"] == 3


def test_replace_query_rejects_whitespace_without_pausing(client, tracked):
    monitor_id = add_monitor(client, tracked, query="task manager")
    response = client.post(f"/api/monitors/{monitor_id}/replace", json={"query": "task manager "})
    assert response.status_code == 422
    with client.app.state.service.db.session() as session:
        assert session.get(Monitor, monitor_id).enabled
        assert len(list(session.scalars(select(Monitor)))) == 1
        assert session.scalar(select(Run)).status == "queued"


def test_replace_query_round_trip_resumes_the_original_series(client, tracked):
    original = add_monitor(client, tracked, query="task manager")
    first = client.post(f"/api/monitors/{original}/replace", json={"query": "todo list"})
    assert first.status_code == 200
    replacement = first.json()["id"]
    second = client.post(f"/api/monitors/{replacement}/replace", json={"query": "task manager"})
    assert second.status_code == 200 and second.json()["id"] == original
    with client.app.state.service.db.session() as session:
        assert session.get(Monitor, original).enabled
        assert not session.get(Monitor, replacement).enabled
        active = list(session.scalars(select(Run).where(Run.status == "queued")))
        assert len(active) == 1 and active[0].monitor_id == original
    assert client.app.state.worker.process_one()


def test_replace_query_ignores_archived_competitor(client, tracked):
    monitor_id = add_monitor(client, tracked, source="google_ai_mode")
    with client.app.state.service.db.session.begin() as session:
        competitor = App(name="Archived competitor", archived=True)
        session.add(competitor)
        session.flush()
        session.add(Target(monitor_id=monitor_id, app_id=competitor.id))
    response = client.post(f"/api/monitors/{monitor_id}/replace", json={"query": "best task apps"})
    assert response.status_code == 200
    with client.app.state.service.db.session() as session:
        assert list(
            session.scalars(select(Target.app_id).where(Target.monitor_id == response.json()["id"]))
        ) == [tracked]
