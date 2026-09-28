import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import TEST_PASSWORD, FakeGateway, sign_in
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from apptrail.api import create_app
from apptrail.auth import SESSION_SECONDS, digest
from apptrail.db import App, AuthLimit, Database, LoginSession, Monitor, Owner, Run, now

HEADERS = {"Content-Type": "application/json", "X-AppTrail-Request": "1"}


@pytest.fixture
def anonymous(tmp_path, monkeypatch):
    for key in ["APPTRAIL_ORIGIN", "SERPAPI_KEY", "SERPAPI_API_KEY"]:
        monkeypatch.delenv(key, raising=False)
    FakeGateway.calls = []
    with TestClient(
        create_app(tmp_path, start_worker=False, gateway_factory=FakeGateway)
    ) as client:
        yield client


def test_first_setup_requires_secret_and_closes_registration(anonymous):
    client = anonymous
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert "Create your account" not in client.get("/").text  # rendered after status loads
    assert client.get("/api/auth/status").json() == {"setup_required": True, "authenticated": False}
    body = {"username": "owner", "password": TEST_PASSWORD, "setup_token": "wrong"}
    assert client.post("/api/auth/setup", json=body, headers=HEADERS).status_code == 403
    token = client.app.state.auth.setup_token()
    assert token not in client.get("/api/auth/status").text
    response = sign_in(client)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert "max-age=86400" in cookie
    assert not client.app.state.auth.setup_path.exists()
    assert client.get("/api/state").status_code == 200
    assert client.post("/api/auth/setup", json={**body, "setup_token": token}).status_code == 409
    with client.app.state.service.db.session() as session:
        owner = session.get(Owner, 1)
        assert owner.username == "owner" and owner.password_hash.startswith("$argon2id$")
        saved = session.scalar(select(LoginSession))
        assert saved.token_hash == digest(client.cookies.get(client.app.state.auth.cookie))
    assert TEST_PASSWORD not in client.get("/api/state").text


def test_every_backend_route_blocks_anonymous_before_provider_use(anonymous):
    client = anonymous
    protected = []
    for route in client.app.routes:
        if route.path.startswith("/api/") and not route.path.startswith("/api/auth/"):
            path = route.path
            for parameter in ["app_id", "monitor_id", "run_id"]:
                path = path.replace("{" + parameter + "}", "1")
            for method in route.methods - {"HEAD", "OPTIONS"}:
                response = client.request(method, path, headers=HEADERS, json={})
                assert response.status_code == 401, (method, path, response.text)
                assert response.headers["cache-control"] == "no-store"
                protected.append((method, path))
    assert len(protected) >= 20
    for path in [
        "/static/app.js",
        "/static/index.html",
        "/api/backup",
        "/api/export.csv",
        "/openapi.json",
        "/docs",
    ]:
        assert client.get(path).status_code == 401
    assert FakeGateway.calls == []
    with client.app.state.service.db.session() as session:
        assert session.scalar(select(Run)) is None
    sign_in(client)
    # An independent browser cannot reuse the logged-in client's server-side state.
    with TestClient(client.app) as other:
        assert other.get("/api/state").status_code == 401
        assert other.post("/api/check", headers=HEADERS).status_code == 401


def test_password_and_validation_errors_do_not_echo_secrets(anonymous):
    secret = "SENSITIVE-password-value"
    response = anonymous.post(
        "/api/auth/setup",
        json={"username": "<bad>", "password": secret, "setup_token": secret},
        headers=HEADERS,
    )
    assert response.status_code == 422 and secret not in response.text
    body = {
        "username": "owner",
        "password": "short7!",
        "setup_token": anonymous.app.state.auth.setup_token(),
    }
    assert anonymous.post("/api/auth/setup", json=body, headers=HEADERS).status_code == 422
    assert anonymous.get("/api/auth/status").json()["setup_required"]
    sign_in(anonymous, password="eight8!x")
    assert anonymous.get("/api/state").status_code == 200


def test_csrf_fetch_metadata_and_content_type_enforced(client):
    assert client.post("/api/check", headers={"X-CSRF-Token": ""}).status_code == 403
    assert client.post("/api/check", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/api/check", headers={"X-AppTrail-Request": ""}).status_code == 403
    assert client.post("/api/check", headers={"Content-Type": "text/plain"}).status_code == 403
    assert client.post("/api/check", headers={"Origin": "https://testserver"}).status_code == 200
    assert client.post("/api/check", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert (
        client.post(
            "/api/check", headers={"Origin": "https://evil.test", "X-CSRF-Token": ""}
        ).status_code
        == 403
    )
    assert client.post("/api/check", headers={"Origin": "http://testserver"}).status_code == 200
    client.cookies.clear()
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "owner", "password": TEST_PASSWORD},
            headers={"X-AppTrail-Request": ""},
        ).status_code
        == 403
    )


def test_blocked_client_cannot_exhaust_other_clients_login_budget(client):
    with TestClient(client.app, client=("192.0.2.50", 4000)) as attacker:
        for _ in range(105):
            response = attacker.post(
                "/api/auth/login",
                headers=HEADERS,
                json={"username": "owner", "password": "incorrect"},
            )
        assert response.status_code == 429
    with TestClient(client.app, client=("192.0.2.51", 4000)) as owner:
        sign_in(owner)
        assert owner.get("/api/state").status_code == 200


def test_ipv6_address_rotation_in_one_subnet_shares_a_login_budget(client):
    for index in range(105):
        with TestClient(client.app, client=(f"2001:db8::a:{index:x}", 4000)) as attacker:
            response = attacker.post(
                "/api/auth/login",
                headers=HEADERS,
                json={"username": "owner", "password": "incorrect"},
            )
            assert response.status_code == (401 if index < 10 else 429)
    with TestClient(client.app, client=("2001:db8:1::1", 4000)) as owner:
        sign_in(owner)


@pytest.mark.parametrize("operation", ["setup", "change", "recovery"])
@pytest.mark.parametrize(
    "password",
    ["short7!", "a" * 127 + "1!", "letters!", "letters1", "letters1 ", "letters1é"],
    ids=["too-short", "too-long", "no-number", "no-special", "space-only", "unicode-letter"],
)
def test_password_requirements_are_enforced_server_side(anonymous, operation, password):
    client = anonymous
    auth = client.app.state.auth
    if operation == "setup":
        response = client.post(
            "/api/auth/setup",
            json={"username": "owner", "password": password, "setup_token": auth.setup_token()},
            headers=HEADERS,
        )
        assert response.status_code == 422
        assert password not in response.text
        assert not auth.has_owner()
        assert auth.setup_path.exists()
        return

    sign_in(client)
    with auth.db.session() as session:
        original_hash = session.get(Owner, 1).password_hash
    if operation == "change":
        response = client.post(
            "/api/auth/password", json={"current_password": TEST_PASSWORD, "new_password": password}
        )
        assert response.status_code == 422
        assert password not in response.text
    else:
        with pytest.raises(HTTPException) as error:
            auth.reset_password(password)
        assert error.value.status_code == 422
        assert password not in str(error.value.detail)
    with auth.db.session() as session:
        assert session.get(Owner, 1).password_hash == original_hash
    assert client.get("/api/state").status_code == 200


@pytest.mark.parametrize("password", ["a" * 126 + "1!", " éight1! "])
def test_password_requirements_preserve_valid_long_and_unicode_passwords(anonymous, password):
    sign_in(anonymous, password=password)
    assert anonymous.post("/api/auth/logout").status_code == 200
    sign_in(anonymous, password=password)
    assert anonymous.get("/api/state").status_code == 200


def test_login_session_rotation_logout_and_forgery(client):
    auth = client.app.state.auth
    first = dict(client.cookies)
    sign_in(client)
    assert dict(client.cookies) != first
    second = dict(client.cookies)
    client.cookies.clear()
    client.cookies.update(first)
    assert client.get("/api/state").status_code == 401
    client.cookies.clear()
    client.cookies.set(auth.cookie, "forged-token")
    assert client.get("/api/state").status_code == 401
    client.cookies.clear()
    for key, value in second.items():
        client.cookies.set(key, value, domain="testserver.local", path="/")
    assert client.post("/api/auth/logout").status_code == 200
    assert not client.cookies.get(auth.cookie)
    client.cookies.update(second)
    assert client.get("/api/state").status_code == 401


def test_session_expires_at_24_hours_even_with_recent_activity(client, monkeypatch):
    response = sign_in(client)
    assert "Max-Age=86400" in response.headers["set-cookie"]
    with client.app.state.service.db.session() as session:
        saved = session.scalar(select(LoginSession))
        assert saved.expires_at - saved.last_seen == SESSION_SECONDS == 86400
        expires_at = saved.expires_at
    monkeypatch.setattr("apptrail.auth.now", lambda: expires_at - 1)
    assert client.get("/api/state").status_code == 200
    monkeypatch.setattr("apptrail.auth.now", lambda: expires_at)
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/check").status_code == 401
    assert client.get("/api/auth/status").json()["authenticated"] is False


@pytest.mark.parametrize("idle_seconds", [31 * 60, 24 * 3600 - 1])
def test_session_survives_idle_time_and_browser_reopen(client, monkeypatch, idle_seconds):
    with client.app.state.service.db.session() as session:
        saved = session.scalar(select(LoginSession))
        issued_at, expires_at = saved.last_seen, saved.expires_at
    cookie = next(iter(client.cookies.jar))
    assert cookie.discard is False and cookie.expires is not None
    assert abs(cookie.expires - expires_at) < 2
    monkeypatch.setattr("apptrail.auth.now", lambda: issued_at + idle_seconds)
    with TestClient(client.app) as reopened:
        reopened.cookies.update({cookie.name: cookie.value})
        reopened.headers.update(client.headers)
        assert reopened.get("/api/state").status_code == 200
        assert reopened.post("/api/check").status_code == 200
    with client.app.state.service.db.session() as session:
        assert session.scalar(select(LoginSession)).expires_at == expires_at


def test_sessions_survive_restart_and_other_browser_logins(client, tmp_path):
    cookies = dict(client.cookies)
    with TestClient(
        create_app(tmp_path, start_worker=False, gateway_factory=FakeGateway)
    ) as restarted:
        restarted.cookies.update(cookies)
        restarted.headers.update(client.headers)
        assert restarted.get("/api/state").status_code == 200
        assert restarted.post("/api/check").status_code == 200
        assert not restarted.get("/api/auth/status").json()["setup_required"]
        with TestClient(restarted.app) as other_browser:
            sign_in(other_browser)
            assert other_browser.get("/api/state").status_code == 200
        assert restarted.get("/api/state").status_code == 200
        assert client.get("/api/state").status_code == 200


def test_downloaded_backup_excludes_sessions_without_signing_out_owner(client, tmp_path):
    response = client.get("/api/backup")
    assert response.status_code == 200
    path = tmp_path / "downloaded.sqlite3"
    path.write_bytes(response.content)
    with sqlite3.connect(path) as backup:
        assert backup.execute("SELECT count(*) FROM login_sessions").fetchone()[0] == 0
        assert backup.execute("SELECT username FROM owner").fetchone()[0] == "owner"
    assert client.get("/api/state").status_code == 200


def test_password_change_revokes_other_sessions_and_requires_current_password(client):
    auth = client.app.state.auth
    cookies = dict(client.cookies)
    client.cookies.clear()
    sign_in(client)
    new_password = "eight8!x"
    assert (
        client.post(
            "/api/auth/password",
            json={"current_password": TEST_PASSWORD, "new_password": "short7!"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/auth/password", json={"current_password": "wrong", "new_password": new_password}
        ).status_code
        == 401
    )
    assert client.get("/api/state").status_code == 200
    response = client.post(
        "/api/auth/password", json={"current_password": TEST_PASSWORD, "new_password": new_password}
    )
    assert response.status_code == 200
    current = dict(client.cookies)
    assert current != cookies
    client.cookies.clear()
    client.cookies.update(cookies)
    assert client.get("/api/state").status_code == 401
    client.cookies.clear()
    assert (
        client.post(
            "/api/auth/login", json={"username": "owner", "password": TEST_PASSWORD}
        ).status_code
        == 401
    )
    sign_in(client, password=new_password)
    assert client.get("/api/state").status_code == 200
    assert client.cookies.get(auth.cookie)


def test_login_throttling_persists_restart(anonymous, tmp_path):
    for _ in range(10):
        response = anonymous.post(
            "/api/auth/login", json={"username": "owner", "password": "wrong"}, headers=HEADERS
        )
        assert response.status_code == 401
    with TestClient(create_app(tmp_path, start_worker=False)) as restarted:
        response = restarted.post(
            "/api/auth/login",
            json={"username": "owner", "password": "wrong"},
            headers={**HEADERS, "X-Forwarded-For": "different-ip"},
        )
        assert response.status_code == 429 and response.headers["retry-after"]
        with restarted.app.state.service.db.session.begin() as session:
            for item in session.scalars(select(AuthLimit)):
                item.started_at = now() - 301
        assert (
            restarted.post(
                "/api/auth/login", json={"username": "owner", "password": "wrong"}, headers=HEADERS
            ).status_code
            == 401
        )


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.0.2.10:8080",
        "http://[2001:db8::10]:8080",
        "http://trail.example.com",
        "https://trail.example.com",
        "https://trail.example.com:8443",
    ],
)
def test_deployment_auth_without_origin_configuration(tmp_path, monkeypatch, origin):
    monkeypatch.delenv("APPTRAIL_ORIGIN", raising=False)
    with TestClient(
        create_app(tmp_path, start_worker=False, gateway_factory=FakeGateway), base_url=origin
    ) as client:
        assert client.get("/login").status_code == 200
        assert client.get("/api/state").status_code == 401
        assert client.get("/api/auth/status").json()["setup_required"]
        assert (
            client.post(
                "/api/auth/setup",
                headers=HEADERS,
                json={"username": "owner", "password": TEST_PASSWORD, "setup_token": "wrong"},
            ).status_code
            == 403
        )
        response = sign_in(client)
        secure = origin.startswith("https://")
        cookie = next(iter(client.cookies))
        assert cookie.startswith("__Host-") == secure
        assert ("Secure" in response.headers["set-cookie"]) == secure
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert "Domain=" not in response.headers["set-cookie"]
        assert client.get("/api/state").status_code == 200
        assert client.post("/api/check", headers={"Origin": origin}).status_code == 200
        assert client.post("/api/check", headers={"X-CSRF-Token": ""}).status_code == 403
        assert (
            client.post(
                "/api/check", headers={"Origin": "https://evil.test", "X-CSRF-Token": ""}
            ).status_code
            == 403
        )
        assert (
            client.post("/api/check", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        )
        assert client.get("/healthz", headers={"Host": "127.0.0.1:8080"}).status_code == 200
        token = client.cookies.get(cookie)
        response = client.post("/api/auth/logout")
        assert response.status_code == 200
        assert not client.cookies.get(cookie)
        assert response.headers["set-cookie"].startswith(cookie + "=")
        assert ("Secure" in response.headers["set-cookie"]) == secure
        assert client.get("/api/state", headers={"Cookie": f"{cookie}={token}"}).status_code == 401
        assert not client.get("/api/auth/status").json()["setup_required"]
        sign_in(client)
        assert client.get("/api/state").status_code == 200


@pytest.mark.parametrize(
    "other_origin",
    [
        "http://192.0.2.20:8080",
        "http://192.0.2.10:8081",
        "https://192.0.2.10:8080",
    ],
)
def test_sessions_remain_bound_to_request_origin(anonymous, other_origin):
    client = anonymous
    client.base_url = "http://192.0.2.10:8080"
    sign_in(client)
    cookie, token = next(iter(client.cookies.items()))
    if other_origin.startswith("https://"):
        cookie = "__Host-" + cookie
    assert client.get(other_origin + "/login").status_code == 200
    assert (
        client.get(other_origin + "/api/state", headers={"Cookie": f"{cookie}={token}"}).status_code
        == 401
    )
    assert client.get("/api/state").status_code == 200


def test_legacy_origin_setting_does_not_restrict_deployment(tmp_path, monkeypatch):
    monkeypatch.setenv("APPTRAIL_ORIGIN", "https://old.example.com")
    with TestClient(
        create_app(tmp_path, start_worker=False), base_url="http://192.0.2.10:8080"
    ) as client:
        sign_in(client)
        assert client.get("/api/state").status_code == 200


def test_setup_race_creates_only_one_owner(anonymous):
    body = {
        "username": "owner",
        "password": TEST_PASSWORD,
        "setup_token": anonymous.app.state.auth.setup_token(),
    }

    def create(_):
        return anonymous.post("/api/auth/setup", json=body, headers=HEADERS).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, range(2))) == [200, 409]
    with anonymous.app.state.service.db.session() as session:
        assert len(session.scalars(select(Owner)).all()) == 1


def test_upgrade_keeps_data_and_blocks_queued_work_until_claimed(tmp_path, monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "test-not-real")
    db = Database(tmp_path)
    with db.session.begin() as session:
        session.add(App(name="Existing app"))
        monitor = Monitor(query="old query", source="apple_app_store", signature="test")
        session.add(monitor)
        session.flush()
        session.add(Run(monitor_id=monitor.id, params={"source": "apple_app_store"}))
    db.close()
    with sqlite3.connect(tmp_path / "apptrail.sqlite3") as connection:
        for table in ["owner", "login_sessions", "auth_limits"]:
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=2")
    with TestClient(
        create_app(tmp_path, start_worker=False, gateway_factory=FakeGateway)
    ) as client:
        assert client.get("/api/state").status_code == 401
        client.app.state.service.dispatch()
        assert client.app.state.worker.process_one() is False
        sign_in(client)
        assert client.get("/api/state").json()["apps"][0]["name"] == "Existing app"
        assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1


def test_server_side_password_recovery_preserves_workspace(client):
    with client.app.state.service.db.session.begin() as session:
        session.add(App(name="Keep me"))
    with pytest.raises(HTTPException) as error:
        client.app.state.auth.reset_password("short7!")
    assert error.value.status_code == 422
    assert client.get("/api/state").status_code == 200
    new = "eight8!x"
    client.app.state.auth.reset_password(new)
    assert client.get("/api/state").status_code == 401
    sign_in(client, password=new)
    assert client.get("/api/state").json()["apps"][0]["name"] == "Keep me"


def test_signing_out_preserves_authorized_scheduled_checks(client, tracked):
    from conftest import add_monitor

    add_monitor(client, tracked)
    assert client.post("/api/auth/logout").status_code == 200
    assert client.post("/api/check").status_code == 401
    assert client.app.state.worker.process_one()
    sign_in(client)
    assert client.get("/api/dashboard").json()["observations"][0]["data"]["position"] == 3


def test_recovery_cli_prompts_and_keeps_account(tmp_path, monkeypatch, capsys):
    from apptrail.cli import main

    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        sign_in(client)
    monkeypatch.setattr("sys.argv", ["apptrail", "--reset-password", "--data-dir", str(tmp_path)])
    monkeypatch.setattr("getpass.getpass", lambda _: "eight8!x")
    main()
    assert "Password reset for owner" in capsys.readouterr().out
    with TestClient(create_app(tmp_path, start_worker=False)) as restarted:
        sign_in(restarted, password="eight8!x")
        assert restarted.get("/api/state").status_code == 200
