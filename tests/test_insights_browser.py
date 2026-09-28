"""Opt-in browser workflows: APPTRAIL_BROWSER_TESTS=1 uv run pytest -m browser."""

import os
import socket
import threading
import time

import pytest
import uvicorn
from conftest import TEST_PASSWORD, FakeGateway, add_monitor
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
from test_competitors import competitor
from test_listing_images import listing_images as listing_images

from apptrail.db import Notification, Observation, Run

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.getenv("APPTRAIL_BROWSER_TESTS") != "1", reason="Opt-in local browser check"
    ),
]


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel=os.getenv("APPTRAIL_BROWSER_CHANNEL", "chrome") or None
        )
        yield browser
        browser.close()


@pytest.fixture
def browser_page(client, browser):
    server = uvicorn.Server(uvicorn.Config(client.app, log_level="error"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started, "Test server did not start within five seconds"
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(error.stack or str(error)))
            try:
                yield page, f"http://127.0.0.1:{port}"
                assert not errors, errors
            finally:
                context.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "Test server failed to stop"


def login(page, url):
    page.goto(url + "/login")
    page.get_by_role("textbox", name="Username").fill("owner")
    page.get_by_label("Password", exact=True).fill(TEST_PASSWORD)
    page.locator("#auth-submit").click()
    page.wait_for_url(url + "/")


def test_distribution_opens_matching_queries_and_country_matrix(
    client, tracked, browser_page, tmp_path
):
    rival = competitor(client)
    for country in ("us", "gb"):
        monitor = add_monitor(client, tracked, country=country)
        client.post(
            f"/api/apps/{tracked}/competitors",
            json={"existing_app_id": rival, "monitor_ids": [monitor]},
        )
        assert client.app.state.worker.process_one()
    page, url = browser_page
    login(page, url)
    page.get_by_role("button", name="Distribution", exact=True).click()
    expect(page.locator(".distribution-bar button").first).to_be_visible()
    page.locator("[data-action=distribution-bucket]").first.click()
    expect(page.locator(".distribution-entries")).to_contain_text("task manager")
    page.get_by_role("button", name="Close dialog").click()
    page.locator("[data-action=tracked-apps]").first.click()
    page.get_by_role("button", name="Open query matrix").click()
    expect(page.locator(".query-matrix tbody tr")).to_have_count(2)
    expect(page.locator(".query-matrix")).to_contain_text("United States")
    expect(page.locator(".query-matrix")).to_contain_text("United Kingdom")
    page.screenshot(path=str(tmp_path / "matrix.png"), animations="disabled")


def test_closing_notifications_during_mark_read_does_not_reopen_dialog(
    client, tracked, browser_page
):
    add_monitor(client, tracked)
    assert client.app.state.worker.process_one()
    with client.app.state.service.db.session.begin() as session:
        run = session.scalar(select(Run))
        session.add(
            Notification(
                app_id=tracked,
                run_id=run.id,
                data={
                    "before": 3,
                    "after": 12,
                    "query": "task manager",
                    "country": "us",
                    "source": "apple_app_store",
                },
            )
        )
    page, url = browser_page
    login(page, url)
    page.locator("#notification-bell").click()
    expect(page.locator(".notification-item")).to_be_visible()
    held_requests = []
    page.route("**/api/notifications/read", lambda route: held_requests.append(route))
    with page.expect_request("**/api/notifications/read"):
        page.get_by_role("button", name="Mark all as read").click()
    page.get_by_role("button", name="Close dialog").click()
    expect(page.locator("#modal")).to_be_hidden()
    assert len(held_requests) == 1
    with page.expect_response("**/api/notifications/read"):
        held_requests[0].continue_()
    expect(page.locator("#notification-count")).to_be_hidden()
    expect(page.locator("#modal")).to_be_hidden()
    assert client.get("/api/state").json()["notification_unread"] == 0


def test_unchanged_polls_reuse_history_and_new_checks_refresh_it(client, tracked, browser_page):
    monitor_id = add_monitor(client, tracked)
    assert client.app.state.worker.process_one()
    page, url = browser_page
    dashboard_requests = []
    page.on(
        "request",
        lambda request: (
            dashboard_requests.append(request.url) if "/api/dashboard?" in request.url else None
        ),
    )
    page.clock.install()
    with page.expect_response("**/api/dashboard?*"):
        login(page, url)
    expect(page.get_by_role("button", name="Distribution", exact=True)).to_be_visible()
    assert len(dashboard_requests) == 1

    # Seeing the next poll proves the previous sync finished without another history request.
    for _ in range(2):
        with page.expect_response("**/api/state"):
            page.clock.run_for(5000)
    assert len(dashboard_requests) == 1

    assert client.post(f"/api/monitors/{monitor_id}/check").status_code == 200
    assert client.app.state.worker.process_one()
    with page.expect_response("**/api/dashboard?*"):
        page.clock.run_for(5000)
    assert len(dashboard_requests) == 2


def test_saved_provider_position_is_text_in_overview_comparison_and_evidence(
    client, tracked, browser_page
):
    monitor = add_monitor(client, tracked)
    rival = competitor(client)
    client.post(
        f"/api/apps/{tracked}/competitors",
        json={"existing_app_id": rival, "monitor_ids": [monitor]},
    )
    assert client.app.state.worker.process_one()
    position = '<b data-provider-injection="position">7</b>'
    with client.app.state.service.db.session.begin() as session:
        run = session.scalar(select(Run))
        run.result = {
            **run.result,
            "items": [{**run.result["items"][0], "position": position}],
        }
        observation = session.scalar(select(Observation))
        observation.data = {**observation.data, "position": position}

    page, url = browser_page
    login(page, url)
    expect(page.locator("#main .rank-number")).to_have_text("#" + position)
    expect(page.locator("[data-provider-injection]")).to_have_count(0)
    page.locator("[data-action=tracked-apps]").first.click()
    expect(page.locator(".comparison-values .rank-number")).to_have_text("#" + position)
    expect(page.locator("[data-provider-injection]")).to_have_count(0)
    page.get_by_role("button", name="View saved check").click()
    expect(page.locator("#modal tbody tr td").first).to_have_text("#" + position)
    expect(page.locator("[data-provider-injection]")).to_have_count(0)


def test_listing_diff_and_mobile_tracking_controls(
    client, tracked, monkeypatch, browser_page, tmp_path
):
    original = FakeGateway.product
    description = {"value": "Plan your day alone."}

    def product(self, *args):
        result = original(self, *args)
        self.responses = [{"data": {"description": description["value"]}}]
        return result

    monkeypatch.setattr(FakeGateway, "product", product)
    listing = client.get("/api/state").json()["apps"][0]["listings"][0]
    watch = client.post(
        "/api/listing-watches", json={"listing_id": listing["id"], "country": "us"}
    ).json()["id"]
    assert client.app.state.worker.process_one()
    description["value"] = "Plan your day together."
    client.post(f"/api/listing-watches/{watch}/check")
    assert client.app.state.worker.process_one()
    client.patch(f"/api/listing-watches/{watch}", json={"enabled": False})
    page, url = browser_page
    login(page, url)
    page.get_by_role("link", name="Listing history", exact=True).click()
    expect(page.locator(".timeline-event")).to_have_count(2)
    page.locator(".timeline-event").first.click()
    expect(page.locator(".diff-text ins")).to_have_text("together.")
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(tmp_path / "listing-diff-mobile.png"), animations="disabled")
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_role("button", name="Close dialog").click()
    page.get_by_role("button", name="Track a listing", exact=True).click()
    page.locator("#history-country").select_option("gb")
    page.locator("[name=frequency]").select_option("daily")
    expect(page.locator("#listing-estimate")).to_contain_text("30 credits per month")
    page.get_by_role("button", name="Enable & save first snapshot").click()
    expect(page.locator("#modal")).to_be_hidden()
    expect(page.locator(".listing-status")).to_contain_text("Daily")
    expect(page.locator(".listing-status")).to_contain_text("Check queued")
    page.get_by_role("button", name="Manage", exact=True).click()
    page.get_by_role("button", name="Pause tracking", exact=True).click()
    expect(page.locator("#modal")).to_be_hidden()
    expect(page.locator(".listing-status")).to_contain_text("Collection paused")
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("platform,language", [("ios", "auto"), ("android", "pt-br")])
def test_listing_setup_submits_store_language_and_saves_baseline(
    client, tracked, browser_page, platform, language
):
    listings = {
        listing["platform"]: str(listing["id"])
        for listing in client.get("/api/state").json()["apps"][0]["listings"]
    }
    page, url = browser_page
    login(page, url)
    page.get_by_role("link", name="Listing history", exact=True).click()
    page.get_by_role("button", name="Track a listing", exact=True).click()
    page.locator("#history-listing").select_option(listings["android"])
    page.locator("#history-language-field input").fill("pt-br")
    page.locator("#history-listing").select_option(listings[platform])
    page.locator("#history-country").select_option("us")
    page.locator("[name=frequency]").select_option("daily")
    if platform == "ios":
        expect(page.locator("#history-language-field")).to_be_hidden()
        expect(page.locator("#history-language-field input")).to_be_disabled()
    else:
        expect(page.locator("#history-language-field input")).to_be_enabled()
        page.locator("#history-language-field input").fill(language)
    with page.expect_response("**/api/listing-watches") as response:
        page.get_by_role("button", name="Enable & save first snapshot").click()
    assert response.value.request.post_data_json["language"] == language
    assert response.value.status == 201, response.value.text()
    watch_id = response.value.json()["id"]
    expect(page.locator("#modal")).to_be_hidden()
    assert client.app.state.worker.process_one()
    snapshots = client.get(f"/api/listing-watches/{watch_id}/history").json()["snapshots"]
    assert len(snapshots) == 1 and snapshots[0]["baseline"]
    page.reload()
    expect(page.locator(".timeline-event")).to_contain_text("First snapshot")


def test_listing_image_metadata_and_reorder_labels(
    client, tracked, listing_images, browser_page, tmp_path
):
    from test_insights import watch
    from test_listing_images import collect, image_bytes

    watch_id = watch(client, platform="android")
    collect(client, watch_id, first=True)
    listing_images["content"] = image_bytes(metadata=True)
    assert collect(client, watch_id)["changes"] == []
    listing_images["content"] = image_bytes()
    listing_images["screenshots"] = ["two", "one"]
    assert collect(client, watch_id)["changes"] == ["screenshots"]
    page, url = browser_page
    login(page, url)
    page.get_by_role("link", name="Listing history", exact=True).click()
    expect(page.locator(".timeline-event")).to_have_count(2)
    page.locator(".timeline-event").first.click()
    expect(page.locator(".diff-field h3")).to_have_text("Screenshots")
    expect(page.locator(".diff-before figcaption")).to_have_text(["1 · Previous", "2 · Previous"])
    expect(page.locator(".diff-after figcaption")).to_have_text(
        ["1 · Moved from 2", "2 · Moved from 1"]
    )
    page.wait_for_function("""() => [...document.querySelectorAll('.diff-field img')]
        .every(img => img.complete && img.naturalWidth > 0)""")
    page.screenshot(path=str(tmp_path / "image-comparison.png"), animations="disabled")
