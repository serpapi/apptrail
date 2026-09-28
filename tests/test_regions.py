import csv
import io

import pytest
from sqlalchemy import select

from apptrail.api import DiscoveryInput, MonitorInput
from apptrail.db import App, Monitor, Run, Target, now
from apptrail.regions import REGIONS, source_countries


def entry(app_id, country="us", source="apple_app_store", **extra):
    return {
        "query": "task manager",
        "app_ids": [app_id],
        "country": country,
        "source": source,
        **extra,
    }


def batch(client, entries):
    response = client.post("/api/monitors/batch", json={"monitors": entries})
    assert response.status_code == 201, response.text
    return response.json()


def test_every_offered_country_passes_source_validation():
    for source in ("apple_app_store", "google_play", "google_ai_mode", "google_ai_overview"):
        for country in source_countries(source):
            assert (
                MonitorInput(source=source, query="weather", country=country, app_ids=[1]).country
                == country
            )
    for platform in ("ios", "android"):
        for country in source_countries("apple_app_store" if platform == "ios" else "google_play"):
            assert (
                DiscoveryInput(platform=platform, query="weather", country=country).country
                == country
            )


def test_discovery_rejects_unsupported_store_countries_before_provider_use(client):
    from conftest import FakeGateway

    before = len(FakeGateway.calls)
    unsupported = next(country for country in REGIONS["google"] if country not in REGIONS["apple"])
    for platform, country in [("ios", unsupported), ("ios", "zz"), ("android", "zz")]:
        assert (
            client.post(
                "/api/discover", json={"platform": platform, "country": country, "query": "app"}
            ).status_code
            == 422
        )
    assert len(FakeGateway.calls) == before


@pytest.mark.parametrize(
    "source,country",
    [
        ("apple_app_store", "li"),
        ("google_play", "kp"),
        ("google_play", "sy"),
        ("google_ai_overview", "cn"),
        ("google_ai_overview", "cu"),
        ("google_ai_overview", "ir"),
    ],
)
def test_unconfirmed_country_pairings_are_not_offered_or_queued(client, tracked, source, country):
    from conftest import FakeGateway

    assert country not in source_countries(source)
    before = len(FakeGateway.calls)
    response = client.post("/api/monitors", json=entry(tracked, country, source))
    assert response.status_code == 422
    assert len(FakeGateway.calls) == before
    assert client.get("/api/dashboard").json()["runs"] == []
    if source in {"apple_app_store", "google_play"}:
        platform = "ios" if source == "apple_app_store" else "android"
        assert (
            client.post(
                "/api/discover", json={"platform": platform, "country": country, "query": "app"}
            ).status_code
            == 422
        )
    else:
        assert (
            MonitorInput(
                source="google_ai_mode", query="apps", country=country, app_ids=[tracked]
            ).country
            == country
        )


def test_multiple_regions_reuse_searches_and_preserve_schedule(client, tracked):
    specs = [
        entry(tracked, c, s)
        for c in ("us", "gb", "in")
        for s in ("apple_app_store", "google_play", "google_ai_mode", "bing_copilot")
    ]
    result = batch(client, specs)
    assert result["created"] == 10
    assert len({item["id"] for item in result["monitors"]}) == 10
    state = client.get("/api/state").json()
    assert len(state["monitors"]) == 10
    assert state["estimated_monthly"]["min"] == 300  # Ten daily searches.
    assert {m["country"] for m in state["monitors"]} == {"us", "gb", "in", "global"}
    old = result["monitors"][0]["id"]
    client.patch(f"/api/monitors/{old}", json={"enabled": False, "frequency": "weekly"})
    repeated = batch(client, specs)
    assert repeated["created"] == 0
    assert repeated["monitors"][0]["frequency"] == "weekly"
    assert len(client.get("/api/dashboard").json()["runs"]) == 10
    assert not next(m for m in client.get("/api/state").json()["monitors"] if m["id"] == old)[
        "enabled"
    ]


def test_batch_is_atomic_when_a_later_app_is_invalid(client, tracked):
    response = client.post(
        "/api/monitors/batch", json={"monitors": [entry(tracked, "us"), entry(999999, "gb")]}
    )
    assert response.status_code == 422
    assert client.get("/api/state").json()["monitors"] == []
    assert client.get("/api/dashboard").json()["runs"] == []


@pytest.mark.parametrize("country", ["zz", "global", "af"])
def test_unsupported_store_country_rejects_whole_batch(client, tracked, country):
    response = client.post(
        "/api/monitors/batch", json={"monitors": [entry(tracked, "us"), entry(tracked, country)]}
    )
    assert response.status_code == 422
    assert client.get("/api/state").json()["monitors"] == []


def test_country_filters_history_export_and_manual_checks(client, tracked):
    specs = [
        entry(tracked, c, s)
        for c in ("us", "gb")
        for s in ("apple_app_store", "google_play", "bing_copilot")
    ]
    batch(client, specs)
    worker = client.app.state.worker
    for _ in range(5):
        assert worker.process_one()
    query = f"app_id={tracked}&country=gb&source=google_play"
    data = client.get("/api/dashboard?" + query).json()
    assert len(data["observations"]) == len(data["runs"]) == 1
    assert data["observations"][0]["params"]["country"] == "gb"
    assert data["observations"][0]["data"]["found"]
    assert data["profiles"] == []  # Original listing snapshots are US.
    exported = list(csv.DictReader(io.StringIO(client.get("/api/export.csv?" + query).text)))
    assert len(exported) == 1
    assert exported[0]["country"] == "gb" and exported[0]["source"] == "google_play"
    assert len(client.get("/api/dashboard?country=global").json()["observations"]) == 1
    assert client.get("/api/dashboard?country=fr").json() == {
        "observations": [],
        "profiles": [],
        "runs": [],
    }
    result = client.post("/api/check?" + query).json()
    assert len(result["run_ids"]) == 1
    with client.app.state.service.db.session() as session:
        pending = session.scalars(select(Run).where(Run.status == "queued")).all()
        assert len(pending) == 1 and pending[0].params["country"] == "gb"
        assert pending[0].params["source"] == "google_play"


def test_country_filter_happens_before_history_limit(client, tracked):
    result = batch(client, [entry(tracked, "gb"), entry(tracked, "us")])
    british, american = [m["id"] for m in result["monitors"]]
    with client.app.state.service.db.session.begin() as session:
        for i in range(45):
            session.add(
                Run(
                    monitor_id=american,
                    status="success",
                    params={"country": "us", "source": "apple_app_store"},
                    created_at=now() - 1 + i / 100,
                )
            )
    data = client.get("/api/dashboard?country=gb").json()
    assert len(data["runs"]) == 1 and data["runs"][0]["monitor_id"] == british


def test_country_batch_reuses_shared_results_for_competitors(client, tracked):
    batch(client, [entry(tracked, "us", "google_ai_mode"), entry(tracked, "gb", "google_ai_mode")])
    assert client.app.state.worker.process_one()
    assert client.app.state.worker.process_one()
    with client.app.state.service.db.session.begin() as session:
        rival = App(name="Competitor", aliases=[], website="")
        session.add(rival)
        session.flush()
        rival_id = rival.id
    result = batch(client, [entry(rival_id, c, "google_ai_mode") for c in ("us", "gb")])
    assert result["created"] == 0
    rows = client.get(f"/api/dashboard?app_id={rival_id}&country=gb").json()["observations"]
    assert len(rows) == 1 and rows[0]["retrospective"]
    with client.app.state.service.db.session() as session:
        assert len(session.scalars(select(Monitor)).all()) == 2
        assert len(session.scalars(select(Target)).all()) == 4


def test_region_catalog_and_batch_require_login(client, tracked):
    catalog = client.get("/api/regions").json()
    assert len(catalog["countries"]) > 200
    assert len(catalog["apple"]) >= 150
    assert set(catalog["apple"]).issubset(catalog["countries"])
    assert catalog["countries"]["gb"] == "United Kingdom"
    assert (
        client.post(
            "/api/monitors/batch",
            json={"monitors": [entry(tracked)]},
            headers={"X-CSRF-Token": "wrong"},
        ).status_code
        == 403
    )
    client.cookies.clear()
    assert client.get("/api/regions").status_code == 401
    assert (
        client.post("/api/monitors/batch", json={"monitors": [entry(tracked)]}).status_code == 401
    )
