import contextlib
import os
import re
import socket
import subprocess
import sys
import time

import httpx
import pytest
from conftest import TEST_PASSWORD, sign_in


@contextlib.contextmanager
def server(tmp_path, extra=(), *, open_browser=False, env_overrides=None, crash=False):
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"SERPAPI_KEY", "SERPAPI_API_KEY", "APPTRAIL_ORIGIN"}
    }
    env["APPTRAIL_DATA_DIR"] = str(tmp_path / "shared")
    env.update(env_overrides or {})
    with (tmp_path / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "apptrail.cli",
                *([] if open_browser else ["--no-browser"]),
                *extra,
            ],
            cwd=tmp_path,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            for _ in range(100):
                log.seek(0)
                match = re.search(r"Web UI: (http://[^\s]+)", log.read())
                if match:
                    url = match.group(1)
                    try:
                        if httpx.get(url + "/healthz", timeout=0.2).status_code == 200:
                            yield url
                            return
                    except httpx.TransportError:
                        pass
                if process.poll() is not None:
                    log.seek(0)
                    raise AssertionError(log.read())
                time.sleep(0.1)
            raise AssertionError("CLI did not become ready in ten seconds")
        finally:
            if crash:
                process.kill()
            else:
                process.terminate()
            process.wait(timeout=15)


def test_free_port_static_assets_and_shared_data(tmp_path):
    with server(tmp_path) as url, httpx.Client(base_url=url) as client:
        assert client.get("/").status_code == 303
        assert client.get("/api/state").status_code == 401
        sign_in(client, setup_token=(tmp_path / "shared" / "setup-token").read_text())
        assert client.get("/").status_code == 200
        assert "AppTrail" in client.get("/static/app.js").text
        assert (
            client.post(
                "/api/auth/password",
                json={"current_password": TEST_PASSWORD, "new_password": "cli-persistent1!"},
            ).status_code
            == 200
        )
        first = client.get("/api/state").json()
        cookie = dict(client.cookies)
    with server(tmp_path) as url, httpx.Client(base_url=url, cookies=cookie) as client:
        assert client.get("/api/state").status_code == 401
        sign_in(client, password="cli-persistent1!")
        second = client.get("/api/state").json()
        assert first["data_directory"] == second["data_directory"]
        assert client.get("/api/auth/status").json()["username"] == "owner"


@pytest.mark.skipif(os.name != "posix", reason="POSIX socket reuse behavior")
@pytest.mark.parametrize("crash", [False, True], ids=["normal-shutdown", "crash"])
def test_immediate_fixed_port_restart(tmp_path, crash):
    with server(tmp_path, crash=crash) as url:
        port = httpx.URL(url).port
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.sendall(
                b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            response = b""
            # Read EOF so the server closes first and leaves its connection in TIME_WAIT.
            while chunk := connection.recv(4096):
                response += chunk
            assert response.startswith(b"HTTP/1.1 200")

    with server(tmp_path, extra=("--port", str(port))) as restarted_url:
        assert restarted_url == url
        assert httpx.get(restarted_url + "/healthz").json()["status"] == "ok"


@pytest.mark.parametrize("host", ["192.0.2.10:8080", "public.example"])
def test_remote_binding_supports_setup_and_login(tmp_path, host):
    with (
        server(tmp_path, extra=("--host", "0.0.0.0")) as url,
        httpx.Client(base_url=url, headers={"Host": host, "Origin": f"http://{host}"}) as client,
    ):
        assert client.get("/login").status_code == 200
        assert client.get("/api/state").status_code == 401
        response = sign_in(client, setup_token=(tmp_path / "shared" / "setup-token").read_text())
        assert "Secure" not in response.headers["set-cookie"]
        assert client.get("/api/state").status_code == 200
        assert client.post("/api/onboarding", json={"action": "dismiss"}).status_code == 200
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/state").status_code == 401
        sign_in(client)
        assert client.get("/api/state").status_code == 200


@pytest.mark.parametrize("trusted_proxy", [True, False], ids=["trusted", "unconfigured"])
def test_https_proxy_auth_with_optional_forwarded_header_trust(tmp_path, trusted_proxy):
    headers = {
        "Host": "trail.example.com",
        "Origin": "https://trail.example.com",
        "X-Forwarded-Proto": "https",
        "Sec-Fetch-Site": "same-origin",
    }
    with (
        server(
            tmp_path, env_overrides={"FORWARDED_ALLOW_IPS": "127.0.0.1" if trusted_proxy else ""}
        ) as url,
        httpx.Client(base_url=url, headers=headers) as client,
    ):
        response = sign_in(client, setup_token=(tmp_path / "shared" / "setup-token").read_text())
        assert response.headers["set-cookie"].startswith("__Host-") == trusted_proxy
        assert ("Secure" in response.headers["set-cookie"]) == trusted_proxy
        # Model the proxy forwarding the browser's cookie over its HTTP upstream.
        cookie, token = next(iter(client.cookies.items()))
        client.headers["Cookie"] = f"{cookie}={token}"
        assert client.get("/api/state").status_code == 200
        assert client.post("/api/onboarding", json={"action": "dismiss"}).status_code == 200
        assert (
            client.post(
                "/api/onboarding", json={"action": "dismiss"}, headers={"X-CSRF-Token": ""}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/onboarding",
                json={"action": "dismiss"},
                headers={"Sec-Fetch-Site": "cross-site"},
            ).status_code
            == 403
        )
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/state").status_code == 401
        client.headers.pop("Cookie")
        client.cookies.clear()
        sign_in(client)
        cookie, token = next(iter(client.cookies.items()))
        client.headers["Cookie"] = f"{cookie}={token}"
        assert client.get("/api/state").status_code == 200


def test_browser_opens_ready_server_on_selected_port(tmp_path):
    capture = tmp_path / "capture_browser.py"
    output = tmp_path / "opened-url.txt"
    capture.write_text(
        "import pathlib, sys, urllib.request\n"
        "with urllib.request.urlopen(sys.argv[1] + '/healthz', timeout=3) as response:\n"
        "    assert response.status == 200\n"
        f"pathlib.Path({str(output)!r}).write_text(sys.argv[1])\n"
    )
    browser = f'"{sys.executable}" "{capture}" %s'
    with server(tmp_path, open_browser=True, env_overrides={"BROWSER": browser}) as url:
        for _ in range(50):
            if output.exists():
                break
            time.sleep(0.1)
        assert output.read_text() == url


def test_second_process_cannot_use_same_workspace(tmp_path):
    with server(tmp_path) as url:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "apptrail.cli",
                "--no-browser",
                "--data-dir",
                str(tmp_path / "shared"),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode != 0
        assert "already running with this data directory" in result.stderr
        assert httpx.get(url + "/healthz").status_code == 200


def test_occupied_explicit_port_has_clear_error(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "apptrail.cli",
                "--port",
                str(listener.getsockname()[1]),
                "--no-browser",
                "--data-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    assert result.returncode == 1
    assert "Could not listen" in result.stderr
