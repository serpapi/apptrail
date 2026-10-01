import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from conftest import TEST_PASSWORD, FakeGateway, add_monitor, sign_in
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_insights import watch
from test_listing_images import collect
from test_listing_images import listing_images as listing_images

from apptrail import backups
from apptrail.api import create_app
from apptrail.auth import hash_password
from apptrail.db import App, ListingAsset, ListingSnapshot, LoginSession, Owner, Run, Setting

HEADERS = {
    "Content-Type": "application/vnd.sqlite3",
    "X-AppTrail-Confirm-Restore": "overwrite",
}


@pytest.fixture
def fresh_workspace(tmp_path):
    with TestClient(
        create_app(tmp_path / "fresh", start_worker=False, gateway_factory=FakeGateway)
    ) as client:
        client.headers.update(HEADERS | {"X-AppTrail-Request": "1"})
        client.headers["X-AppTrail-Setup-Token"] = client.app.state.auth.setup_token()
        yield client


def test_setup_restore_uses_backup_account_and_closes_setup(client, tracked, fresh_workspace):
    backup = client.get("/api/backup").content
    fresh = fresh_workspace
    token = fresh.app.state.auth.setup_token()
    response = fresh.post("/api/auth/restore", content=backup)
    assert response.status_code == 200, response.text
    assert fresh.get("/api/auth/status").json() == {
        "setup_required": False,
        "authenticated": False,
    }
    assert not fresh.app.state.auth.setup_path.exists()
    assert fresh.get("/api/state").status_code == 401
    assert fresh.post("/api/auth/restore", content=backup).status_code == 409
    sign_in(fresh)
    assert fresh.get("/api/state").json()["apps"][0]["name"] == "Todo Example"
    assert fresh.app.state.service.config.api_key == ""
    assert (
        fresh.post(
            "/api/auth/setup",
            json={
                "username": "intruder",
                "password": TEST_PASSWORD,
                "setup_token": token,
            },
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"X-AppTrail-Setup-Token": ""}, 403),
        ({"X-AppTrail-Setup-Token": "wrong"}, 403),
        ({"X-AppTrail-Request": ""}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Content-Type": "text/plain"}, 403),
        ({"X-AppTrail-Confirm-Restore": ""}, 422),
    ],
)
def test_setup_restore_checks_access_before_reading_upload(fresh_workspace, headers, status):
    fresh = fresh_workspace
    response = fresh.post("/api/auth/restore", content=b"bad", headers=headers)
    assert response.status_code == status
    assert fresh.get("/api/auth/status").json()["setup_required"]
    assert fresh.app.state.auth.setup_token()
    assert not list(fresh.app.state.service.config.directory.glob(".restore-*"))


def test_setup_restore_rejects_invalid_files_and_limits_attempts(fresh_workspace):
    fresh = fresh_workspace
    assert fresh.post("/api/auth/restore", content=b"bad").status_code == 422
    for _ in range(9):
        assert (
            fresh.post(
                "/api/auth/restore",
                content=b"bad",
                headers={
                    "X-AppTrail-Setup-Token": "wrong",
                },
            ).status_code
            == 403
        )
    assert fresh.post("/api/auth/restore", content=b"bad").status_code == 429
    assert fresh.get("/api/auth/status").json()["setup_required"]


def test_setup_restore_cannot_replace_an_existing_account(client, tracked):
    response = client.post(
        "/api/auth/restore",
        content=b"bad",
        headers=HEADERS
        | {
            "X-AppTrail-Setup-Token": "anything",
        },
    )
    assert response.status_code == 409
    assert client.get("/api/state").json()["apps"][0]["id"] == tracked


def test_setup_restore_rechecks_account_after_validation(
    client,
    tracked,
    fresh_workspace,
    monkeypatch,
):
    content = client.get("/api/backup").content
    entered, release = threading.Event(), threading.Event()
    original = backups.prepare_restore

    def prepare(*args):
        path = original(*args)
        entered.set()
        assert release.wait(10)
        return path

    monkeypatch.setattr("apptrail.api.prepare_restore", prepare)
    fresh = fresh_workspace
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(fresh.post, "/api/auth/restore", content=content)
        try:
            assert entered.wait(5)
            sign_in(fresh, username="new-owner")
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 409
    assert fresh.get("/api/auth/status").json()["username"] == "new-owner"
    assert fresh.get("/api/state").json()["apps"] == []


def restore(client, content, **headers):
    return client.post("/api/restore", content=content, headers=HEADERS | headers)


def changed_backup(client, tmp_path, sql, parameters=()):
    path = tmp_path / "edited.sqlite3"
    path.write_bytes(client.get("/api/backup").content)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(sql, parameters)
        connection.commit()
    return path.read_bytes()


def test_restore_replaces_data_and_password_revokes_every_session(client, tracked, tmp_path):
    add_monitor(client, tracked)
    assert client.app.state.worker.process_one()
    db = client.app.state.service.db
    with db.session.begin() as session:
        session.add(ListingAsset(id="image", mime="image/png", content=b"saved-image"))
        session.merge(Setting(key="sync_paused", value=True))
    # Include live sessions to exercise restoration from a manually copied database too.
    raw = tmp_path / "raw.sqlite3"
    db.backup(raw)
    with closing(sqlite3.connect(raw)) as connection:
        connection.execute(
            "UPDATE owner SET username=?, password_hash=?",
            ("restored-owner", hash_password("restored password 2!")),
        )
        connection.commit()
    with TestClient(client.app) as other:
        sign_in(other)
        old_cookie = client.cookies.get(client.app.state.auth.cookie)
        assert (
            client.patch(f"/api/apps/{tracked}", json={"name": "Current name"}).status_code == 200
        )
        with db.session.begin() as session:
            session.add(App(name="Only in current database"))
        response = restore(client, raw.read_bytes())
        assert response.status_code == 200, response.text
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert client.get("/api/state").status_code == 401
        assert other.get("/api/state").status_code == 401
        client.cookies.set(client.app.state.auth.cookie, old_cookie)
        assert client.get("/api/state").status_code == 401
        client.cookies.clear()
        assert (
            client.post(
                "/api/auth/login", json={"username": "owner", "password": TEST_PASSWORD}
            ).status_code
            == 401
        )
        sign_in(client, username="restored-owner", password="restored password 2!")
        state = client.get("/api/state").json()
        assert [app["name"] for app in state["apps"]] == ["Todo Example"]
        assert state["sync_paused"] is True
        assert client.app.state.service.config.api_key == "test-credential-not-a-real-key"
        with db.session() as session:
            assert session.get(ListingAsset, "image").content == b"saved-image"
            assert session.scalar(select(Run)).status == "success"
            assert len(list(session.scalars(select(LoginSession)))) == 1
        (recovery,) = (tmp_path / "backups").glob("before-restore-*.sqlite3")
        with closing(sqlite3.connect(recovery)) as connection:
            assert connection.execute("SELECT username FROM owner").fetchone() == ("owner",)
            assert connection.execute("SELECT count(*) FROM apps").fetchone() == (2,)
            assert connection.execute("SELECT count(*) FROM login_sessions").fetchone() == (0,)
        assert recovery.stat().st_mode & 0o777 == 0o600
        assert not list(tmp_path.glob(".restore-*"))


@pytest.mark.parametrize("body", [b"", b"not a database", b"SQLite format 3\0" + b"\0" * 100])
def test_invalid_upload_preserves_workspace_and_login(client, tracked, tmp_path, body):
    response = restore(client, body)
    assert response.status_code == 422, response.text
    assert client.get("/api/state").json()["apps"][0]["id"] == tracked
    assert not list(tmp_path.glob(".restore-*"))
    assert not list(tmp_path.glob("backups/before-restore-*"))


@pytest.mark.parametrize(
    "sql",
    [
        "PRAGMA user_version=999",
        "PRAGMA user_version=5",
        "DROP TABLE observations",
        "ALTER TABLE apps DROP COLUMN name",
        "CREATE TRIGGER surprise AFTER INSERT ON apps BEGIN DELETE FROM owner; END",
        "DELETE FROM owner",
        "UPDATE owner SET password_hash='bad-hash'",
        "UPDATE listings SET app_id=98765",
        "UPDATE apps SET aliases='invalid-json'",
        "UPDATE apps SET aliases='null'",
        "UPDATE listings SET metadata_json='[]'",
    ],
)
def test_incompatible_backup_is_rejected_before_changes(client, tracked, tmp_path, sql):
    content = changed_backup(client, tmp_path, sql)
    response = restore(client, content)
    assert response.status_code == 422, response.text
    assert client.get("/api/state").json()["apps"][0]["name"] == "Todo Example"
    assert not list(tmp_path.glob("backups/before-restore-*"))


@pytest.mark.parametrize("username", ["Owner", "a" * 65, b"owner", "ab", "bad name", "owner\n"])
@pytest.mark.parametrize("setup", [False, True])
def test_restore_rejects_unusable_usernames(
    client, tracked, fresh_workspace, tmp_path, username, setup
):
    content = changed_backup(client, tmp_path, "UPDATE owner SET username=?", (username,))
    if setup:
        response = fresh_workspace.post("/api/auth/restore", content=content)
        assert response.status_code == 422, response.text
        assert fresh_workspace.get("/api/auth/status").json()["setup_required"]
        assert fresh_workspace.app.state.auth.setup_token()
    else:
        response = restore(client, content)
        assert response.status_code == 422, response.text
        assert client.get("/api/state").json()["apps"][0]["id"] == tracked
        assert not list(tmp_path.glob("backups/before-restore-*"))
        client.cookies.clear()
        sign_in(client)
    assert "username" in response.json()["detail"]


@pytest.mark.parametrize("username", ["a.b_c-1", "a" * 64])
def test_restored_canonical_username_can_sign_in(client, tmp_path, username):
    content = changed_backup(client, tmp_path, "UPDATE owner SET username=?", (username,))
    response = restore(client, content)
    assert response.status_code == 200, response.text
    sign_in(client, username=username.upper())
    assert client.get("/api/auth/status").json()["username"] == username


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"X-CSRF-Token": "wrong"}, 403),
        ({"X-AppTrail-Request": ""}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Content-Type": "multipart/form-data"}, 403),
        ({"X-AppTrail-Confirm-Restore": ""}, 422),
    ],
)
def test_restore_requires_csrf_and_explicit_overwrite(client, headers, status):
    assert restore(client, b"bad", **headers).status_code == status
    assert client.get("/api/state").status_code == 200


def test_upload_limit_checks_declared_and_streamed_size(client, monkeypatch, tmp_path):
    monkeypatch.setattr("apptrail.api.MAX_RESTORE_BYTES", 16)
    assert restore(client, b"x" * 17).status_code == 413
    assert restore(client, iter([b"x" * 10, b"x" * 10])).status_code == 413
    assert not list(tmp_path.glob(".restore-*"))
    assert client.get("/api/state").status_code == 200


def test_compact_backup_shrinks_file_and_restores_history_with_omission_notices(
    client,
    tracked,
    listing_images,
    tmp_path,
):
    watch_id = watch(client, platform="android")
    baseline = collect(client, watch_id, first=True)
    before = client.get(f"/api/listing-snapshots/{baseline['id']}").json()["snapshot"]
    monitor_id = add_monitor(client, tracked)
    assert client.app.state.worker.process_one()
    db = client.app.state.service.db
    with db.session.begin() as session:
        run = session.scalar(select(Run).where(Run.monitor_id == monitor_id))
        run.responses = [{"data": {"large": "RAW_RESPONSE_TO_OMIT" * 100_000}}]
        run_id, result = run.id, run.result
        session.add(ListingAsset(id="f" * 64, mime="image/png", content=b"IMAGE_TO_OMIT" * 100_000))
    full = tmp_path / "full.sqlite3"
    db.backup(full)
    content = client.get("/api/backup").content
    compact = tmp_path / "compact.sqlite3"
    compact.write_bytes(content)
    assert compact.stat().st_size < full.stat().st_size / 4
    assert b"RAW_RESPONSE_TO_OMIT" not in content and b"IMAGE_TO_OMIT" not in content
    with closing(sqlite3.connect(compact)) as saved:
        assert saved.execute("SELECT count(*) FROM listing_assets").fetchone() == (0,)
        assert saved.execute(
            "SELECT count(*) FROM run_payloads WHERE responses != '[]'"
        ).fetchone() == (0,)
        assert saved.execute("PRAGMA freelist_count").fetchone() == (0,)
        assert saved.execute("PRAGMA quick_check").fetchone() == ("ok",)
    original = client.get(f"/api/runs/{run_id}").json()
    assert original["responses"] and not original["responses_omitted"]
    asset_id = before["data"]["icon"]["asset_id"]
    assert client.get(f"/api/listing-assets/{asset_id}").status_code == 200

    assert restore(client, content).status_code == 200
    sign_in(client)
    restored = client.get(f"/api/runs/{run_id}").json()
    assert restored["result"] == result
    assert restored["observations"] == original["observations"]
    assert restored["responses"] == [] and restored["responses_omitted"]
    snapshot = client.get(f"/api/listing-snapshots/{baseline['id']}").json()["snapshot"]
    assert snapshot["data"] == before["data"]
    assert snapshot["images_omitted"] and asset_id in snapshot["missing_assets"]
    assert client.get(f"/api/listing-assets/{asset_id}").status_code == 404
    assert client.post(f"/api/apps/{tracked}/reanalyze").json()["updated"] == 1

    # New checks archive images again and compare against the retained hashes.
    after = collect(client, watch_id)
    assert after["changes"] == []
    comparison = client.get(f"/api/listing-snapshots/{after['id']}").json()
    assert comparison["snapshot"]["missing_assets"] == []
    assert not comparison["snapshot"]["images_omitted"]
    assert not comparison["previous"]["images_omitted"]
    assert client.get(f"/api/listing-assets/{asset_id}").status_code == 200
    new_run = client.post(f"/api/monitors/{monitor_id}/check").json()["run_id"]
    assert client.app.state.worker.process_one()
    checked = client.get(f"/api/runs/{new_run}").json()
    assert checked["responses"] and not checked["responses_omitted"]
    assert client.get(f"/api/runs/{run_id}").json()["responses_omitted"]

    again = tmp_path / "again.sqlite3"
    again.write_bytes(client.get("/api/backup").content)
    with closing(sqlite3.connect(again)) as saved:
        omitted = json.loads(
            saved.execute("SELECT value FROM settings WHERE key='backup_omissions'").fetchone()[0]
        )
        assert omitted["images_through_snapshot"] == after["id"]
    assert restore(client, again.read_bytes()).status_code == 200
    sign_in(client)
    assert client.get(f"/api/runs/{new_run}").json()["responses_omitted"]
    assert client.get(f"/api/runs/{run_id}").json()["responses_omitted"]


def test_missing_image_without_backup_marker_is_not_mislabelled(client, tracked, listing_images):
    baseline = collect(client, watch(client, platform="android"), first=True)
    db = client.app.state.service.db
    with db.session.begin() as session:
        asset_id = session.get(ListingSnapshot, baseline["id"]).data["icon"]["asset_id"]
        session.delete(session.get(ListingAsset, asset_id))
    saved = client.get(f"/api/listing-snapshots/{baseline['id']}").json()["snapshot"]
    assert asset_id in saved["missing_assets"]
    assert not saved["images_omitted"]


def test_legacy_removal_cutoffs_do_not_guess_why_a_response_is_missing(client, tmp_path):
    db = client.app.state.service.db
    with db.session.begin() as session:
        run = Run(status="error", error="Provider returned no response")
        session.add(run)
        session.flush()
        run_id = run.id
        session.merge(Setting(key="backup_omissions", value={"responses_through_run": run_id}))
        session.merge(Setting(key="storage_cleanup", value={"responses": run.created_at + 1}))
    legacy = tmp_path / "legacy-omissions.sqlite3"
    db.backup(legacy)
    content = legacy.read_bytes()
    for _ in range(2):
        assert restore(client, content).status_code == 200
        sign_in(client)
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["responses"] == []
        assert not saved["responses_omitted"] and not saved["responses_cleaned"]
        content = client.get("/api/backup").content


def test_schema_six_backup_upgrades_in_staging(client, tracked, listing_images, tmp_path):
    baseline = collect(client, watch(client, platform="android"), first=True)
    db = client.app.state.service.db
    legacy = tmp_path / "legacy.sqlite3"
    db.backup(legacy)
    with closing(sqlite3.connect(legacy)) as saved:
        saved.execute("PRAGMA user_version=6")
        data = json.loads(saved.execute("SELECT data FROM listing_snapshots").fetchone()[0])
        data["icon"].pop("pixel_hash")
        saved.execute("UPDATE listing_snapshots SET data=?", (json.dumps(data),))
        saved.commit()
    assert restore(client, legacy.read_bytes()).status_code == 200
    sign_in(client)
    snapshot = client.get(f"/api/listing-snapshots/{baseline['id']}").json()["snapshot"]
    assert snapshot["data"]["icon"]["pixel_hash"]
    assert not snapshot["images_omitted"]
    with closing(sqlite3.connect(db.path)) as saved:
        assert saved.execute("PRAGMA user_version").fetchone() == (7,)


def test_failed_replacement_keeps_data_and_login(client, tracked, monkeypatch):
    content = client.get("/api/backup").content

    def fail(_):
        raise OSError("Disk unavailable")

    monkeypatch.setattr(client.app.state.service.db, "restore", fail)
    with pytest.raises(OSError, match="Disk unavailable"):
        restore(client, content)
    assert client.get("/api/state").json()["apps"][0]["id"] == tracked


def test_restore_blocks_requests_and_waits_for_active_work(client, tracked, monkeypatch):
    content = client.get("/api/backup").content
    service = client.app.state.service
    worker = client.app.state.worker
    entered, release = threading.Event(), threading.Event()
    original = service.state

    def slow_state():
        entered.set()
        assert release.wait(10)
        return original()

    monkeypatch.setattr(service, "state", slow_state)
    exclusive = backups.RestoreGate.exclusive
    waiting = threading.Event()

    def tracked_exclusive(gate):
        waiting.set()
        return exclusive(gate)

    monkeypatch.setattr(backups.RestoreGate, "exclusive", tracked_exclusive)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(client.get, "/api/state")
        assert entered.wait(5)
        restoring = pool.submit(restore, client, content)
        try:
            assert waiting.wait(5)
            assert not restoring.done()
            assert client.get("/healthz").status_code == 200
            assert client.get("/api/auth/status").status_code == 503
        finally:
            release.set()
        assert reading.result(timeout=5).status_code == 200
        assert restoring.result(timeout=5).status_code == 200
    assert worker.thread is None


def test_restore_waits_for_worker_and_restarts_it(client, tracked, monkeypatch):
    db = client.app.state.service.db
    worker = client.app.state.worker
    content = client.get("/api/backup").content
    entered, release = threading.Event(), threading.Event()
    calls = []

    def loop():
        calls.append("start")
        entered.set()
        assert release.wait(10)
        with db.session.begin() as session:
            session.add(App(name="Old worker result"))
        calls.append("finish")
        worker.stop_event.wait(10)

    monkeypatch.setattr(worker, "loop", loop)
    worker.start()
    assert entered.wait(5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(restore, client, content)
        try:
            assert worker.stop_event.wait(5)
            assert not pending.done()
            # The restarted worker waits instead of writing another old result.
            monkeypatch.setattr(worker, "loop", lambda: worker.stop_event.wait(10))
            release.set()
            assert pending.result(timeout=5).status_code == 200
            assert not worker.stop_event.is_set()
            assert worker.thread.is_alive()
            with db.session() as session:
                assert list(session.scalars(select(App.name))) == ["Todo Example"]
                assert session.scalar(select(Owner)).username == "owner"
            assert calls == ["start", "finish"]
        finally:
            release.set()
            worker.stop()
