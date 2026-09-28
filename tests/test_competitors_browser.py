import os

import pytest
from conftest import FakeGateway, add_monitor
from playwright.sync_api import expect
from test_competitors import competitor
from test_insights_browser import browser as browser
from test_insights_browser import browser_page as browser_page
from test_insights_browser import login

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.getenv("APPTRAIL_BROWSER_TESTS") != "1", reason="Opt-in local browser check"
    ),
]


def test_competitor_main_page_connects_both_stores_and_selected_countries(
    client, tracked, monkeypatch, browser_page, tmp_path
):
    def discover(self, platform, query, country, language):
        result = self.product(
            platform, "999" if platform == "ios" else "org.rival", country, language
        )
        return [{**result, "title": "TickTick: Tasks and reminders"}]

    monkeypatch.setattr(FakeGateway, "discover", discover)
    for source in ("apple_app_store", "google_play", "google_ai_mode", "google_ai_overview"):
        for country in ("us", "gb"):
            add_monitor(client, tracked, source, country=country)
    add_monitor(client, tracked, "bing_copilot")
    page, url = browser_page
    login(page, url)
    page.locator(".query-competitors[data-action=add-competitor]").first.click()
    expect(page).to_have_url(url + "/#apps")
    expect(page.locator("#modal")).to_be_hidden()
    expect(page.get_by_role("heading", name="Add a competitor", exact=True)).to_be_visible()
    expect(page.locator("#competitor-selection-summary")).to_have_text(
        "4 store searches · 5 AI searches · 2 countries + Global"
    )
    expect(page.locator("[data-competitor-query]:checked")).to_have_count(5)
    page.locator('[data-competitor-country="gb"]').uncheck()
    expect(page.locator("#competitor-selection-summary")).to_have_text(
        "2 store searches · 3 AI searches · 1 country + Global"
    )
    page.locator('[data-competitor-select-all="store"]').click()
    expect(page.locator("#competitor-selection-summary")).to_have_text(
        "0 store searches · 3 AI searches · 1 country + Global"
    )
    page.locator('[data-competitor-select-all="store"]').click()
    for platform in ("ios", "android"):
        form = page.locator(f"#competitor-discover-{platform}")
        form.locator("input").fill("TickTick")
        form.get_by_role("button", name="Search", exact=True).click()
        page.locator(
            f'[data-action="competitor-setup-pick"][data-platform="{platform}"]'
        ).first.click()
    page.get_by_label("Competitor name in your workspace", exact=True).fill("TickTick")
    expect(page.locator("#competitor-save")).to_be_enabled()
    page.locator("#main").screenshot(
        path=str(tmp_path / "competitor-setup-desktop.png"), animations="disabled"
    )
    page.set_viewport_size({"width": 375, "height": 860})
    page.locator("#main").screenshot(
        path=str(tmp_path / "competitor-setup-mobile.png"), animations="disabled"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(page.locator("#competitor-save")).to_be_enabled()
    page.locator("#competitor-save").click()
    expect(page.get_by_role("heading", name="Apps and competitors", exact=True)).to_be_visible()
    expect(page.locator(".competitor-card .competitor-tag")).to_have_text(
        "Competitor · Todo Example"
    )
    expect(page.locator(".competitor-card")).to_contain_text("iOS + Android")
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.locator("#main").screenshot(
        path=str(tmp_path / "competitor-portfolio.png"), animations="disabled"
    )
    state = client.get("/api/state").json()
    rival = next(app for app in state["apps"] if app["name"] == "TickTick")
    assert rival["competitor_for"] == [tracked]
    assert {listing["platform"] for listing in rival["listings"]} == {"ios", "android"}
    for monitor in state["monitors"]:
        assert (rival["id"] in monitor["app_ids"]) == (monitor["country"] in {"us", "global"})
    page.reload()
    expect(page.locator(".competitor-card .competitor-tag")).to_have_text(
        "Competitor · Todo Example"
    )


def test_existing_competitor_requires_compatible_listing_and_keeps_saved_results(
    client, tracked, browser_page
):
    rival = competitor(client)
    ios = add_monitor(client, tracked)
    android = add_monitor(client, tracked, "google_play")
    ai = add_monitor(client, tracked, "google_ai_mode")
    while client.app.state.worker.process_one():
        pass
    before = list(FakeGateway.calls)
    page, url = browser_page
    login(page, url)
    page.goto(url + "/#apps")
    page.locator(".page-heading [data-action=add-competitor]").click()
    page.get_by_role("button", name="Use a workspace app", exact=True).click()
    page.get_by_label("Competitor app", exact=True).select_option(str(rival))
    expect(page.locator("#competitor-selection-issue")).to_contain_text("Connect Google Play")
    expect(page.locator("#competitor-save")).to_be_disabled()
    page.locator(".competitor-query-option").filter(has_text="Google Play").locator(
        "input"
    ).uncheck()
    expect(page.locator("#competitor-save")).to_be_enabled()
    page.locator("#competitor-save").click()
    expect(page.get_by_role("heading", name="Competitors", exact=True)).to_be_visible()
    assert FakeGateway.calls == before
    saved = client.get("/api/state").json()
    assert {
        row["monitor_id"] for row in saved["latest_observations"] if row["app_id"] == rival
    } == {ios, ai}
    assert rival not in next(
        monitor["app_ids"] for monitor in saved["monitors"] if monitor["id"] == android
    )
    page.goto(url + "/#overview")
    expect(page.locator(".competitor-suggestion")).to_have_count(0)
    page.locator(".query-competitors[data-action=tracked-apps]").first.click()
    expect(page.locator("#modal .comparison-row")).to_have_count(2)
    page.get_by_role("button", name="Add a competitor", exact=True).click()
    expect(page).to_have_url(url + "/#apps")
    expect(page.locator("#modal")).to_be_hidden()


def test_suggestion_strips_dismissal_and_navigation_order(client, tracked, browser_page, tmp_path):
    add_monitor(client, tracked)
    page, url = browser_page
    login(page, url)
    expect(page.locator(".competitor-suggestion")).to_be_visible()
    expect(page.locator(".listing-history-suggestion")).to_be_visible()
    page.locator(".listing-history-suggestion").screenshot(
        path=str(tmp_path / "listing-intro.png"), animations="disabled"
    )
    page.get_by_role("button", name="Dismiss listing history introduction").click()
    expect(page.locator(".listing-history-suggestion")).to_have_count(0)
    page.reload()
    expect(page.locator(".competitor-suggestion")).to_be_visible()
    expect(page.locator(".listing-history-suggestion")).to_have_count(0)
    order = page.locator("nav [data-nav]").evaluate_all(
        "items => items.map(item => item.dataset.nav)"
    )
    assert order.index("activity") == order.index("listing-history") + 1
    for route in ("rankings", "ai"):
        page.goto(url + "/#" + route)
        expect(page.locator(".competitor-suggestion")).to_be_visible()
        page.locator(".competitor-suggestion [data-action=add-competitor]").click()
        expect(page).to_have_url(url + "/#apps")
        expect(page.locator("#competitor-parent")).to_have_value(str(tracked))
        page.get_by_role("button", name="Cancel", exact=True).click()
    assert client.get("/api/listing-watches").json()["watches"] == []


def test_app_with_no_queries_can_add_an_existing_competitor(client, tracked, browser_page):
    rival = competitor(client)
    page, url = browser_page
    login(page, url)
    page.locator(".competitor-suggestion [data-action=add-competitor]").click()
    expect(page.locator(".competitor-editor")).to_contain_text("This app has no saved queries yet")
    page.get_by_role("button", name="Use a workspace app", exact=True).click()
    page.get_by_label("Competitor app", exact=True).select_option(str(rival))
    expect(page.locator("#competitor-save")).to_be_enabled()
    page.locator("#competitor-save").click()
    expect(page.locator(".competitor-card .competitor-tag")).to_have_text(
        "Competitor · Todo Example"
    )
