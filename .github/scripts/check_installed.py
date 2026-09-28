"""Boot the installed wheel outside the checkout and exercise its packaged UI."""

import http.cookiejar
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import apptrail

checkout = Path(__file__).resolve().parents[2]
assert not Path(apptrail.__file__).resolve().is_relative_to(checkout / "src"), (
    "This smoke check must run against the installed wheel, not an editable checkout."
)
environment = {
    key: value
    for key, value in os.environ.items()
    if key not in {"SERPAPI_KEY", "SERPAPI_API_KEY", "APPTRAIL_ORIGIN", "PYTHONPATH"}
}

with tempfile.TemporaryDirectory(prefix="apptrail-wheel-") as temporary:
    directory = Path(temporary)
    with (directory / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "apptrail.cli", "--no-browser", "--data-dir", str(directory)],
            cwd=directory,
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 15
            url = None
            while time.monotonic() < deadline:
                log.seek(0)
                output = log.read()
                if process.poll() is not None:
                    raise AssertionError(f"Installed app exited during startup:\n{output}")
                match = re.search(r"Web UI: (http://[^\s]+)", output)
                if match:
                    url = match.group(1)
                    try:
                        with urllib.request.urlopen(url + "/healthz", timeout=0.5) as response:
                            if json.load(response)["status"] == "ok":
                                break
                    except (OSError, urllib.error.URLError):
                        pass
                time.sleep(0.05)
            else:
                raise AssertionError(f"Installed app did not become ready:\n{output}")

            browser = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
            )
            try:
                browser.open(url + "/api/state", timeout=5)
            except urllib.error.HTTPError as error:
                assert error.code == 401
            else:
                raise AssertionError("The installed app exposed anonymous workspace data")
            request = urllib.request.Request(
                url + "/api/auth/setup",
                data=json.dumps(
                    {
                        "username": "package-check",
                        "password": "Package smoke test password 1!",
                        "setup_token": (directory / "setup-token").read_text(),
                    }
                ).encode(),
                headers={"Content-Type": "application/json", "X-AppTrail-Request": "1"},
            )
            with browser.open(request, timeout=5) as response:
                assert json.load(response)["csrf_token"]
            with browser.open(url + "/api/state", timeout=5) as response:
                state = json.load(response)
                assert state["apps"] == [] and state["monitors"] == []
                assert state["configured"] is False
            with browser.open(url + "/api/regions", timeout=5) as response:
                assert json.load(response)["countries"]["gb"] == "United Kingdom"
            for path in (
                "/",
                "/static/app.js",
                "/static/history-chart.js",
                "/static/tracking.js",
                "/static/insights-data.js",
                "/static/insights-ui.js",
                "/static/vendor/chart.umd.min.js",
                "/static/vendor/chart-LICENSE.md",
            ):
                with browser.open(url + path, timeout=5) as response:
                    assert response.status == 200 and response.read(), path
            print(
                "Installed wheel passed startup, owner setup, region catalog, state, and UI assets."
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
