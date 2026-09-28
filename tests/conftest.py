import copy
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apptrail.api import create_app
from apptrail.engines import ProviderError, SearchOutcome, canonical_url


class FakeGateway:
    calls = []
    failures = []

    def __init__(self, config):
        self.config = config
        self.responses = []
        self.requests_count = 0

    def account(self, key=None):
        if key == "invalid":
            raise ProviderError("Invalid API key")
        return {"account_status": "Active", "total_searches_left": 1234}

    def product(self, platform, identifier, country, language):
        self.calls.append(("product", identifier))
        self.requests_count += 1
        return {
            "platform": platform,
            "external_id": identifier,
            "bundle_id": "org.example.todo" if platform == "ios" else "",
            "title": "Todo Example",
            "url": canonical_url(platform, identifier, country),
            "icon": "",
            "developer": "Example",
            "country": country,
            "language": language,
            "metadata_json": {"rating": 4.5, "reviews": 100},
        }

    def discover(self, platform, query, country, language):
        return [
            self.product(
                platform, "123456" if platform == "ios" else "org.example.todo", country, language
            )
        ]

    def search(self, spec):
        self.calls.append(("search", spec["source"]))
        self.requests_count = 1
        if self.failures:
            raise self.failures.pop(0)
        if spec["source"] in {"apple_app_store", "google_play"}:
            platform = "ios" if spec["source"] == "apple_app_store" else "android"
            item = self.product(
                platform, "123456" if platform == "ios" else "org.example.todo", "us", "en"
            )
            item.update(position=3, primary=True, section="Search results")
            result = {
                "kind": "store",
                "items": [item],
                "results_checked": 1,
                "answer_available": None,
            }
        else:
            result = {
                "kind": "ai",
                "text": "Todo Example is a task manager.",
                "references": [],
                "answer_available": True,
                "items": [],
            }
        self.responses = [{"params": spec, "data": copy.deepcopy(result)}]
        return SearchOutcome(result, self.responses, self.requests_count)


TEST_PASSWORD = "testing apptrail secure password 1!"


def sign_in(client, setup_token=None, username="owner", password=TEST_PASSWORD):
    client.headers["X-AppTrail-Request"] = "1"
    client.headers["Content-Type"] = "application/json"
    required = client.get("/api/auth/status").json()["setup_required"]
    body = {"username": username, "password": password}
    if required:
        body["setup_token"] = setup_token or client.app.state.auth.setup_token()
    response = client.post("/api/auth/" + ("setup" if required else "login"), json=body)
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response


@pytest.fixture
def client(tmp_path, monkeypatch):
    for name in ("SERPAPI_KEY", "SERPAPI_API_KEY", "APPTRAIL_ORIGIN"):
        monkeypatch.delenv(name, raising=False)
    FakeGateway.calls, FakeGateway.failures = [], []
    application = create_app(tmp_path, start_worker=False, gateway_factory=FakeGateway)
    with TestClient(application) as value:
        sign_in(value)
        yield value


@pytest.fixture
def tracked(client):
    assert (
        client.post("/api/key", json={"api_key": "test-credential-not-a-real-key"}).status_code
        == 200
    )
    candidates = []
    for platform in ("ios", "android"):
        response = client.post("/api/discover", json={"platform": platform, "query": "Todo"})
        candidates.append(response.json()["candidates"][0]["token"])
    response = client.post(
        "/api/apps", json={"name": "Todo Example", "candidate_tokens": candidates}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def add_monitor(client, app_id, source="apple_app_store", query="task manager", **extra):
    response = client.post(
        "/api/monitors", json={"query": query, "source": source, "app_ids": [app_id], **extra}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def discover_live_app(client):
    tokens = []
    for platform, identifier in [("ios", "572688855"), ("android", "com.todoist")]:
        response = client.post("/api/discover", json={"platform": platform, "query": "Todoist"})
        assert response.status_code == 200, response.text
        candidates = response.json()["candidates"]
        selected = next((item for item in candidates if item["external_id"] == identifier), None)
        assert selected is not None, [(item["title"], item["external_id"]) for item in candidates]
        assert selected["developer"] and selected["title"] and selected["url"]
        tokens.append(selected["token"])
    response = client.post(
        "/api/apps",
        json={"name": "Todoist", "candidate_tokens": tokens, "website": "https://todoist.com"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture(scope="session")
def live_app_database(tmp_path_factory):
    if not (os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY")):
        pytest.skip("Set SERPAPI_API_KEY for live integration checks.")
    with TestClient(
        create_app(tmp_path_factory.mktemp("live-template"), start_worker=False)
    ) as client:
        sign_in(client)
        discover_live_app(client)
        backup = client.get("/api/backup")
        assert backup.status_code == 200, backup.text
        return backup.content


@pytest.fixture
def live_client(tmp_path, live_app_database, request):
    setting = (
        "APPTRAIL_REGIONAL_TEST_DIR"
        if request.module.__name__ == "test_regions_live"
        else "APPTRAIL_LIVE_DATA_DIR"
    )
    parent = os.getenv(setting)
    if parent:
        Path(parent).mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=tmp_path.name + "-", dir=parent))
    else:
        directory = tmp_path
    (directory / "apptrail.sqlite3").write_bytes(live_app_database)
    with TestClient(create_app(directory, start_worker=False)) as client:
        sign_in(client)
        yield client
