import os

import pytest
from conftest import add_monitor
from playwright.sync_api import expect
from test_insights_browser import browser as browser
from test_insights_browser import browser_page as browser_page
from test_insights_browser import login

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.getenv("APPTRAIL_BROWSER_TESTS") != "1", reason="Opt-in local browser check"
    ),
]


@pytest.mark.parametrize(
    ("route", "sources"),
    [
        ("rankings", ("apple_app_store", "google_play")),
        ("ai", ("google_ai_mode", "google_ai_overview")),
    ],
)
def test_first_query_expands_and_filtered_management_keeps_full_scope(
    client, tracked, browser_page, route, sources
):
    ids = [
        add_monitor(client, tracked, source=source, country=country, query="First query")
        for source in sources
        for country in ("us", "gb")
    ]
    other = add_monitor(client, tracked, source=sources[0], query="Second query")
    page, url = browser_page
    login(page, url)
    page.goto(url + "/#" + route)
    toggles = page.locator("[data-action=expand-query]")
    expect(toggles).to_have_count(2)
    expect(toggles.first).to_have_attribute("aria-expanded", "true")
    expect(toggles.nth(1)).to_have_attribute("aria-expanded", "false")
    toggles.first.click()
    expect(toggles.first).to_have_attribute("aria-expanded", "false")
    page.locator("#country-filter").select_option("gb")
    expect(toggles).to_have_count(1)
    expect(toggles.first).to_have_attribute("aria-expanded", "false")
    toggles.first.click()
    expect(page.locator(".query-group-details tbody tr")).to_have_count(2)
    page.get_by_role("button", name="Manage First query", exact=True).click()
    expect(page.locator(".query-group-scope")).to_contain_text("All 4 country and source checks")
    page.locator("#query-group-form [name=frequency]").select_option("weekly")
    page.locator("#query-group-form [name=enabled]").select_option("false")
    page.get_by_role("button", name="Save query settings", exact=True).click()
    expect(page.locator("#modal")).not_to_be_visible()
    state = client.get("/api/state").json()
    for monitor in state["monitors"]:
        if monitor["id"] in ids:
            assert monitor["frequency"] == "weekly"
            assert not monitor["enabled"]
        elif monitor["id"] == other:
            assert monitor["frequency"] == "daily"
            assert monitor["enabled"]


def test_active_query_shortcut_and_settings_share_workspace_pause(client, tracked, browser_page):
    add_monitor(client, tracked)
    page, url = browser_page
    initial_state = []

    def hold_initial_state(route):
        if not initial_state:
            initial_state.append(route)
        else:
            route.continue_()

    page.route("**/api/state", hold_initial_state)
    with page.expect_request("**/api/state"):
        login(page, url)
    expect(page.locator("[data-action=search-summary]")).to_be_disabled()
    expect(page.locator("#query-count")).to_have_text("Loading queries…")
    initial_state[0].continue_()
    expect(page.locator("[data-action=search-summary]")).to_be_enabled()
    page.locator("[data-action=search-summary]").click()
    page.get_by_role("button", name="Pause all sync", exact=True).click()
    expect(page.get_by_role("button", name="Resume all sync", exact=True)).to_be_visible()
    assert client.get("/api/state").json()["sync_paused"]
    assert not client.app.state.worker.process_one()
    page.get_by_role("button", name="Sync settings", exact=False).click()
    expect(page.locator("#sync-settings")).to_contain_text("Paused")
    page.get_by_role("button", name="Resume all sync", exact=True).click()
    expect(page.locator("#sync-settings")).to_contain_text("Running")
    assert not client.get("/api/state").json()["sync_paused"]
    assert client.app.state.worker.process_one()
