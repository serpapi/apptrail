from types import SimpleNamespace

import pytest
from conftest import FakeGateway, add_monitor

from apptrail.matching import match_app
from apptrail.worker import Worker


@pytest.mark.parametrize("interruption", ["featured", "secondary", "shallow"])
def test_unranked_checks_preserve_alert_baseline_without_confirming_loss(
    client, tracked, monkeypatch, interruption
):
    original = FakeGateway.search
    current = {"value": 3}

    def search(gateway, spec):
        result = original(gateway, spec)
        value = current["value"]
        result.result["results_checked"] = 50
        if value in {"featured", "secondary"}:
            result.result["items"][0].update(
                position=None if value == "featured" else 1,
                primary=False,
                section="Featured app" if value == "featured" else "Similar apps",
            )
        elif value in {"shallow", "absent"}:
            result.result["items"] = []
            result.result["results_checked"] = 1 if value == "shallow" else 50
        else:
            result.result["items"][0]["position"] = value
        return result

    monkeypatch.setattr(FakeGateway, "search", search)
    monitor_id = add_monitor(client, tracked, source="google_play")
    worker = client.app.state.worker
    assert worker.process_one()
    for value in (12, interruption, "absent"):
        current["value"] = value
        assert client.post(f"/api/monitors/{monitor_id}/check").status_code == 200
        assert worker.process_one()
        assert client.get("/api/notifications").json()["notifications"] == []
    assert client.post(f"/api/monitors/{monitor_id}/check").status_code == 200
    assert worker.process_one()
    notifications = client.get("/api/notifications").json()["notifications"]
    assert len(notifications) == 1
    assert notifications[0]["data"]["before"] == 3
    assert notifications[0]["data"]["after"] is None


@pytest.mark.parametrize(
    "website,reference,expected",
    [
        ("https://todoist.com", "https://www.todoist.com/help", True),
        ("https://www.todoist.com", "https://todoist.com/help", True),
        ("https://www.todoist.com/app", "https://todoist.com/app/features", True),
        ("https://todoist.com/app", "https://www.todoist.com/another-app", False),
        ("https://todoist.com", "https://help.todoist.com", False),
        ("https://todoist.com", "https://todoist.com.attacker.test", False),
    ],
)
def test_website_citations_match_www_variants_and_keep_app_boundaries(website, reference, expected):
    app = SimpleNamespace(name="Todoist", aliases=[], website=website)
    data = match_app(
        app,
        [],
        {
            "kind": "ai",
            "text": "Here are some useful apps.",
            "references": [{"title": "App website", "link": reference}],
            "answer_available": True,
        },
    )
    assert data["cited"] is expected
    assert data["website_cited"] is expected
    assert not data["app_link"]


@pytest.mark.parametrize(
    "prefix",
    ["ß " * 200, "İ " * 200, "ﬃ " * 150, "e\u0301 " * 200],
    ids=["sharp-s", "dotted-i", "ligature", "combining-accent"],
)
def test_ai_evidence_uses_original_offsets_after_unicode_normalization(prefix):
    original_name = "Cafe\u0301 Planner"
    answer = prefix + "Try " + original_name + " today. " + "Details. " * 50
    data = match_app(
        SimpleNamespace(name="Café Planner", aliases=[], website=""),
        [],
        {"kind": "ai", "text": answer, "references": [], "answer_available": True},
    )
    assert data["mentioned"]
    evidence = data["evidence"][0]["text"]
    assert original_name in evidence
    assert evidence in answer
    assert len(evidence) <= len(original_name) + 170


def test_due_checks_start_within_a_minute_without_dispatching_every_worker_tick(monkeypatch):
    clock = {"time": 1_800_000_000}
    due = clock["time"] + 61
    stop_at = due + 61
    dispatches, starts = [], []
    pending = {"value": False}

    class SimulatedStop:
        def is_set(self):
            return clock["time"] >= stop_at

        def wait(self, seconds):
            clock["time"] += seconds

    def dispatch():
        dispatches.append(clock["time"])
        if clock["time"] >= due and not starts:
            pending["value"] = True

    def process_one():
        if pending["value"]:
            starts.append(clock["time"])
            pending["value"] = False
        return False

    worker = Worker(SimpleNamespace(db=None, dispatch=dispatch))
    worker.stop_event = SimulatedStop()
    monkeypatch.setattr("apptrail.worker.now", lambda: clock["time"])
    monkeypatch.setattr(worker, "process_one", process_one)
    worker.loop()
    assert len(starts) == 1
    assert due <= starts[0] <= due + 60
    assert len(dispatches) <= 3
