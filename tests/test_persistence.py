import json
import os
import sqlite3

import pytest
from conftest import add_monitor
from filelock import FileLock
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from apptrail.api import create_app
from apptrail.db import App, Database, Listing, Monitor, Run, now


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
def test_database_and_journals_are_private_in_an_existing_directory(tmp_path):
    tmp_path.chmod(0o755)
    old_umask = os.umask(0o022)
    try:
        db = Database(tmp_path)
        try:
            with db.session.begin() as session:
                session.add(App(name="Private history"))
            files = list(tmp_path.glob("apptrail.sqlite3*"))
            assert any(path.name.endswith("-wal") for path in files)
            assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)
        finally:
            db.close()
    finally:
        os.umask(old_umask)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
def test_existing_journal_permissions_are_repaired(tmp_path):
    db = Database(tmp_path)
    try:
        with db.session.begin() as session:
            session.add(App(name="Preserved history"))
        for path in tmp_path.glob("apptrail.sqlite3*"):
            path.chmod(0o644)
        reopened = Database(tmp_path)
        try:
            assert all(
                path.stat().st_mode & 0o777 == 0o600 for path in tmp_path.glob("apptrail.sqlite3*")
            )
            with reopened.session() as session:
                assert session.scalar(select(App)).name == "Preserved history"
        finally:
            reopened.close()
    finally:
        db.close()


def test_schema_upgrade_preserves_rows_and_creates_backup(tmp_path):
    db = Database(tmp_path)
    with db.session.begin() as session:
        session.add(App(name="Preserved app"))
    db.close()
    with sqlite3.connect(tmp_path / "apptrail.sqlite3") as connection:
        connection.execute("DROP INDEX one_active_search")
        connection.execute("DROP INDEX one_active_profile")
        connection.execute("PRAGMA user_version=1")
    upgraded = Database(tmp_path)
    with upgraded.session() as session:
        assert session.scalar(select(App)).name == "Preserved app"
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1
    with sqlite3.connect(tmp_path / "apptrail.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    upgraded.close()


def test_newer_schema_refuses_downgrade(tmp_path):
    db = Database(tmp_path)
    db.close()
    with sqlite3.connect(tmp_path / "apptrail.sqlite3") as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError, match="newer AppTrail"):
        Database(tmp_path)


def test_competitor_migration_preserves_original_apps_and_backs_up_schema_five(tmp_path):
    db = Database(tmp_path)
    with db.session.begin() as session:
        session.add(App(name="Original app"))
    db.close()
    with sqlite3.connect(tmp_path / "apptrail.sqlite3") as connection:
        connection.execute("DROP TABLE competitors")
        connection.execute("PRAGMA user_version=5")
    upgraded = Database(tmp_path)
    with upgraded.session() as session:
        assert session.scalar(select(App)).name == "Original app"
    with sqlite3.connect(tmp_path / "backups" / "before-schema-5.sqlite3") as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 5
        assert backup.execute("SELECT name FROM apps").fetchone()[0] == "Original app"
    with sqlite3.connect(tmp_path / "apptrail.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM competitors").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    upgraded.close()


def test_payload_migration_preserves_evidence_and_keeps_run_metadata_small(tmp_path):
    db = Database(tmp_path)
    result = {"kind": "ai", "text": "Saved answer"}
    responses = [{"data": "x" * 500_000}]
    with db.session.begin() as session:
        run = Run(status="success", result=result, responses=responses)
        session.add(run)
        session.flush()
        run_id = run.id
    db.close()
    path = tmp_path / "apptrail.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE runs SET result=?, responses=? WHERE id=?",
            (json.dumps(result), json.dumps(responses), run_id),
        )
        connection.execute("DROP TABLE run_payloads")
        connection.execute("DROP INDEX ix_runs_checked_at")
        connection.execute("PRAGMA user_version=3")
    upgraded = Database(tmp_path)
    try:
        with upgraded.session() as session:
            restored = session.get(Run, run_id)
            assert restored.result == result and restored.responses == responses
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT result, responses FROM runs").fetchone() == (
                "{}",
                "[]",
            )
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM runs WHERE COALESCE(started_at, created_at) > ?",
                (0,),
            ).fetchall()
            assert "ix_runs_checked_at" in str(plan)
        with sqlite3.connect(tmp_path / "backups/before-schema-3.sqlite3") as backup:
            assert (
                json.loads(backup.execute("SELECT responses FROM runs").fetchone()[0]) == responses
            )
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        upgraded.close()


def test_workspace_lock_is_checked_before_schema_upgrade(tmp_path):
    db = Database(tmp_path)
    db.close()
    with sqlite3.connect(tmp_path / "apptrail.sqlite3") as connection:
        connection.execute("PRAGMA user_version=3")
    with FileLock(tmp_path / "instance.lock"):
        with pytest.raises(RuntimeError, match="already running"):
            create_app(tmp_path)
    with sqlite3.connect(tmp_path / "apptrail.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    assert not (tmp_path / "backups").exists()


def test_legacy_duplicate_jobs_do_not_block_schema_upgrade(tmp_path):
    db = Database(tmp_path)
    with db.session.begin() as session:
        monitor = Monitor(signature="legacy", query="task", source="apple_app_store")
        session.add(monitor)
        session.flush()
        session.add(Run(monitor_id=monitor.id, params={}))
    db.close()
    with sqlite3.connect(tmp_path / "apptrail.sqlite3") as connection:
        connection.execute("DROP INDEX one_active_search")
        connection.execute(
            "INSERT INTO runs (monitor_id, kind, status, params, result, responses, error, attempts, requests_count, created_at, available_at) VALUES (1,'search','queued','{}','{}','[]','',0,0,?,?)",
            (now(), now()),
        )
        connection.execute("PRAGMA user_version=1")
    upgraded = Database(tmp_path)
    try:
        with upgraded.session() as session:
            assert sorted(session.scalars(select(Run.status))) == ["cancelled", "queued"]
    finally:
        upgraded.close()


def test_shared_search_backfills_without_new_request(client, tracked):
    monitor_id = add_monitor(client, tracked)
    client.app.state.worker.process_one()
    db = client.app.state.service.db
    with db.session.begin() as session:
        app = App(name="Competitor")
        session.add(app)
        session.flush()
        session.add(
            Listing(
                app_id=app.id,
                platform="ios",
                external_id="999",
                title="Competitor",
                url="https://apps.apple.com/us/app/id999",
            )
        )
        competitor_id = app.id
    assert add_monitor(client, competitor_id) == monitor_id
    assert not client.app.state.worker.process_one()
    rows = client.get(f"/api/dashboard?app_id={competitor_id}").json()["observations"]
    assert len(rows) == 1 and rows[0]["retrospective"] and not rows[0]["data"]["found"]


def test_database_prevents_duplicate_active_jobs(client, tracked):
    monitor_id = add_monitor(client, tracked)
    with pytest.raises(IntegrityError), client.app.state.service.db.session.begin() as session:
        session.add(Run(monitor_id=monitor_id, params={}))
        session.flush()


def test_interrupted_job_is_recovered_on_start(client, tracked, monkeypatch):
    add_monitor(client, tracked)
    db, worker = client.app.state.service.db, client.app.state.worker
    with db.session.begin() as session:
        session.scalar(select(Run)).status = "running"
    monkeypatch.setattr(worker, "loop", lambda: None)
    worker.start()
    worker.stop()
    with db.session() as session:
        assert session.scalar(select(Run)).status == "queued"
    assert worker.process_one()


def test_archiving_cancels_exclusive_pending_checks(client, tracked):
    add_monitor(client, tracked)
    response = client.patch(f"/api/apps/{tracked}", json={"name": "Todo Example", "archived": True})
    assert response.status_code == 200
    assert not client.app.state.worker.process_one()
    client.app.state.service.dispatch()
    assert not client.app.state.worker.process_one()
