import os

import pytest
from conftest import add_monitor
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


@pytest.mark.parametrize(
    ("source", "route", "variants", "descriptions"),
    [
        (
            "apple_app_store",
            "rankings",
            [{"device": "mobile", "depth": 1}, {"device": "desktop", "depth": 3}],
            ["Mobile · Up to 50 results", "Desktop · Up to 150 results"],
        ),
        (
            "google_play",
            "rankings",
            [{"depth": 1}, {"depth": 3}],
            ["Up to 1 page", "Up to 3 pages"],
        ),
        (
            "google_ai_mode",
            "ai",
            [{"device": "mobile"}, {"device": "desktop"}],
            ["Mobile", "Desktop"],
        ),
    ],
)
def test_query_variants_identify_the_individual_schedule_control(
    client, tracked, browser_page, source, route, variants, descriptions
):
    ids = [
        add_monitor(client, tracked, source, query="daily planner", **variant)
        for variant in variants
    ]
    page, url = browser_page
    login(page, url)
    page.goto(url + "/#" + route)
    expect(page.locator(".query-group-details")).to_be_visible()
    for monitor_id, description in zip(ids, descriptions, strict=True):
        frequency = page.locator(f'[data-frequency="{monitor_id}"]')
        row = frequency.locator("xpath=ancestor::tr")
        expect(row.locator(".cell-button .query-meta")).to_contain_text(description)
        assert description in frequency.get_attribute("aria-label")
        for action in ("toggle-monitor", "check-one", "add-countries", "edit-query"):
            assert description in row.locator(f'[data-action="{action}"]').get_attribute(
                "aria-label"
            )
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH" and response.url.endswith(f"/api/monitors/{ids[1]}")
        )
    ) as changed:
        page.locator(f'[data-frequency="{ids[1]}"]').select_option("weekly")
    assert changed.value.ok
    state = client.get("/api/state").json()
    assert {monitor["id"]: monitor["frequency"] for monitor in state["monitors"]} == {
        ids[0]: "daily",
        ids[1]: "weekly",
    }


@pytest.mark.parametrize("starting_platform", ["ios", "android"])
def test_switching_between_single_store_apps_keeps_a_usable_query_source(
    client, tracked, browser_page, starting_platform
):
    app_ids = {
        "ios": competitor(client, name="iOS only", platform="ios", identifier="881"),
        "android": competitor(
            client, name="Android only", platform="android", identifier="org.android.only"
        ),
    }
    destination = "android" if starting_platform == "ios" else "ios"
    sources = {"ios": "apple_app_store", "android": "google_play"}
    page, url = browser_page
    login(page, url)
    page.goto(url + "/#rankings")
    page.locator('[data-action="add-store"]').click()
    page.locator("#query-app").select_option(str(app_ids[starting_platform]))
    expect(
        page.locator(f'input[name="sources"][value="{sources[starting_platform]}"]')
    ).to_be_checked()
    page.locator('textarea[name="store_queries"]').fill("store only keyword")
    page.locator('#query-form select[name="frequency"]').select_option("weekly")
    page.locator("#query-app").select_option(str(app_ids[destination]))
    expect(page.locator(f'input[name="sources"][value="{sources[destination]}"]')).to_be_checked()
    expect(page.locator('textarea[name="store_queries"]')).to_have_value("store only keyword")
    expect(page.locator('#query-form select[name="frequency"]')).to_have_value("weekly")
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/api/monitors/batch")
        )
    ) as saved:
        page.locator('#query-form button[type="submit"]').click()
    assert saved.value.ok
    expect(page.locator("#modal")).to_be_hidden()
    monitors = client.get("/api/state").json()["monitors"]
    assert len(monitors) == 1
    assert monitors[0]["app_ids"] == [app_ids[destination]]
    assert monitors[0]["source"] == sources[destination]
    assert monitors[0]["frequency"] == "weekly"


def test_switching_apps_preserves_choices_for_sources_that_remain_available(
    client, tracked, browser_page
):
    ios = competitor(client, name="iOS only", platform="ios", identifier="881")
    page, url = browser_page
    login(page, url)
    page.goto(url + "/#rankings")
    page.locator('[data-action="add-store"]').click()
    page.locator('input[name="sources"][value="apple_app_store"]').uncheck()
    page.locator("#query-app").select_option(str(ios))
    expect(page.locator('input[name="sources"][value="apple_app_store"]')).not_to_be_checked()


def link_competitor(client, parent, rival, monitor_ids=()):
    response = client.post(
        f"/api/apps/{parent}/competitors",
        json={"existing_app_id": rival, "monitor_ids": list(monitor_ids)},
    )
    assert response.status_code == 201, response.text


def archive_app(client, app_id):
    app = next(app for app in client.get("/api/state").json()["apps"] if app["id"] == app_id)
    response = client.patch(
        f"/api/apps/{app_id}",
        json={
            "name": app["name"],
            "aliases": app["aliases"],
            "website": app["website"],
            "archived": True,
        },
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("width", [1440, 375])
def test_unlink_competitor_keeps_dirty_edits_shared_queries_and_history(
    client, tracked, browser_page, width, tmp_path
):
    rival = competitor(client, name="Rival app")
    monitor = add_monitor(client, tracked)
    while client.app.state.worker.process_one():
        pass
    link_competitor(client, tracked, rival, [monitor])
    before = client.get("/api/state").json()
    page, url = browser_page
    page.set_viewport_size({"width": width, "height": 1000})
    login(page, url)
    page.goto(url + "/#apps")
    page.locator(f'[data-action="edit-app"][data-id="{rival}"]').click()
    expect(page.locator("#manage-competitor-links")).to_contain_text("Todo Example")
    page.get_by_label("App name", exact=True).fill("Unsaved rival name")
    page.get_by_label("Aliases · comma-separated", exact=True).fill("Unsaved alias")
    page.get_by_label("App website").fill("https://unsaved.example")
    page.get_by_label("Archive app and pause its automatic checks", exact=True).check()
    patches = []
    page.on(
        "request",
        lambda request: patches.append(request.url) if request.method == "PATCH" else None,
    )
    with page.expect_response(
        lambda response: (
            response.request.method == "DELETE"
            and response.url.endswith(f"/api/apps/{tracked}/competitors/{rival}")
        )
    ) as removed:
        page.get_by_role("button", name="Remove competitor link to Todo Example").click()
    assert removed.value.ok
    expect(page.locator("#manage-competitor-links")).to_contain_text("No competitor links.")
    expect(page.locator("#modal")).to_be_visible()
    expect(page.get_by_label("App name", exact=True)).to_have_value("Unsaved rival name")
    expect(page.get_by_label("Aliases · comma-separated", exact=True)).to_have_value(
        "Unsaved alias"
    )
    expect(page.get_by_label("App website")).to_have_value("https://unsaved.example")
    expect(
        page.get_by_label("Archive app and pause its automatic checks", exact=True)
    ).to_be_checked()
    assert not patches
    after = client.get("/api/state").json()
    app = next(app for app in after["apps"] if app["id"] == rival)
    assert app["competitor_for"] == []
    assert app["name"] == "Rival app" and app["archived"] is False
    assert after["monitors"] == before["monitors"]
    assert after["latest_observations"] == before["latest_observations"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#modal").screenshot(path=str(tmp_path / "unlink-keeps-draft.png"))
    page.get_by_role("button", name="Close dialog", exact=True).click()
    expect(page.locator(".portfolio-section").first).to_contain_text("Rival app")
    expect(page.get_by_role("heading", name="Competitors", exact=True)).to_have_count(0)


def test_archived_parent_links_do_not_classify_apps_but_remain_removable(
    client, tracked, browser_page
):
    rival = competitor(client, name="Rival app")
    active_parent = competitor(client, name="Other parent", identifier="888")
    link_competitor(client, tracked, rival)
    link_competitor(client, active_parent, rival)
    archive_app(client, tracked)
    page, url = browser_page
    login(page, url)
    page.goto(url + "/#apps")
    card = page.locator(".app-card").filter(
        has=page.get_by_role("heading", name="Rival app", exact=True)
    )
    expect(card.locator(".competitor-tag")).to_have_text("Competitor · Other parent")
    archive_app(client, active_parent)
    page.reload()
    expect(page.get_by_role("heading", name="Competitors", exact=True)).to_have_count(0)
    expect(page.locator(".portfolio-section").first).to_contain_text("Rival app")
    expect(card.locator(".competitor-tag")).to_have_count(0)
    card.locator('[data-action="select-app"]').click()
    expect(page.locator(".competitor-suggestion")).to_contain_text("See how Rival app compares")
    page.goto(url + "/#apps")
    page.locator(f'[data-action="edit-app"][data-id="{rival}"]').click()
    expect(page.locator(".competitor-link-list .tag")).to_have_text(["Archived", "Archived"])
    page.get_by_role("button", name="Remove competitor link to Todo Example").click()
    expect(page.locator(".competitor-link-list li")).to_have_count(1)
    expect(page.locator(".competitor-link-list")).to_contain_text("Other parent")
    page.get_by_role("button", name="Remove competitor link to Other parent").click()
    expect(page.locator("#manage-competitor-links")).to_contain_text("No competitor links.")
    app = next(app for app in client.get("/api/state").json()["apps"] if app["id"] == rival)
    assert app["competitor_for"] == [] and app["archived"] is False


@pytest.mark.parametrize("platform", ["ios", "android"])
@pytest.mark.parametrize(
    ("country_filter", "listing_country", "available", "expected_country"),
    [
        ("global", "gb", None, "gb"),
        ("de", "gb", None, "de"),
        ("global", "zz", None, "us"),
        ("global", "zz", ["gb", "de"], "gb"),
    ],
)
def test_listing_setup_uses_a_supported_country_default(
    client,
    tracked,
    browser_page,
    platform,
    country_filter,
    listing_country,
    available,
    expected_country,
):
    from apptrail.db import Listing

    add_monitor(client, tracked, "bing_copilot")
    add_monitor(client, tracked, "google_ai_mode", country="de")
    listing_id = next(
        listing["id"]
        for listing in client.get("/api/state").json()["apps"][0]["listings"]
        if listing["platform"] == platform
    )
    with client.app.state.service.db.session.begin() as session:
        session.get(Listing, listing_id).country = listing_country
    page, url = browser_page
    if available:
        regions = client.get("/api/regions").json()
        regions["apple_app_store" if platform == "ios" else "google_play"] = available
        page.route("**/api/regions", lambda route: route.fulfill(json=regions))
    login(page, url)
    page.goto(url + "/#ai")
    page.locator("#country-filter").select_option(country_filter)
    page.get_by_role("link", name="Listing history", exact=True).click()
    page.get_by_role("button", name="Track a listing", exact=True).click()
    page.locator("#history-listing").select_option(str(listing_id))
    expect(page.locator("#history-country")).to_have_value(expected_country)
    with page.expect_response("**/api/listing-watches") as saved:
        page.get_by_role("button", name="Enable & save first snapshot", exact=True).click()
    assert saved.value.status == 201, saved.value.text()
    assert saved.value.request.post_data_json["country"] == expected_country


def test_competitor_setup_defaults_to_app_with_no_active_parent(client, tracked, browser_page):
    original = competitor(client, name="Now an original", identifier="881")
    competitor(client, name="Another original", identifier="882")
    link_competitor(client, tracked, original)
    archive_app(client, tracked)
    page, url = browser_page
    login(page, url)
    page.goto(url + "/#apps")
    page.locator('.page-heading [data-action="add-competitor"]').click()
    expect(page.locator("#competitor-parent")).to_have_value(str(original))
