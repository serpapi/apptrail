import os

import pytest
from playwright.sync_api import expect
from test_insights_browser import browser as browser  # noqa: F401
from test_insights_browser import browser_page as browser_page  # noqa: F401
from test_insights_browser import login
from test_listing_images import image_bytes
from test_storage import asset_id
from test_storage import stored_history as stored_history

from apptrail.db import ListingAsset

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.getenv("APPTRAIL_BROWSER_TESTS") != "1", reason="Opt-in local browser check"
    ),
]


@pytest.mark.parametrize("width", [1440, 390])
def test_storage_controls_cleanup_and_history_notices(
    client, stored_history, browser_page, tmp_path, width
):
    with client.app.state.service.db.session.begin() as session:
        session.get(ListingAsset, asset_id("shared")).content = image_bytes()
    page, url = browser_page
    page.set_viewport_size({"width": width, "height": 1000})
    login(page, url)
    page.goto(url + "/#settings")
    panel = page.locator("#storage-panel")
    expect(panel).to_contain_text("Total database size")
    expect(panel).to_contain_text("Saved SerpApi responses")
    expect(panel).to_contain_text("Listing-history images")
    assert panel.evaluate("el => el.nextElementSibling.textContent.includes('Data & backups')")
    assert panel.evaluate("el => el.scrollWidth <= el.clientWidth")
    panel.screenshot(path=str(tmp_path / f"storage-{width}.png"))
    responses = page.locator('[data-storage-days="responses"]')
    responses.select_option("30")
    panel.get_by_role("button", name="Delete older responses", exact=True).click()
    expect(page.locator("#modal")).to_contain_text("older than 30 days")
    page.get_by_role("button", name="Cancel", exact=True).click()
    assert client.get(f"/api/runs/{stored_history['runs'][40]}").json()["responses"]
    responses.select_option("7")
    panel.get_by_role("button", name="Delete older responses", exact=True).click()
    expect(page.locator("#modal")).to_contain_text("older than 7 days")
    expect(page.locator("#modal")).to_contain_text("cannot be undone")
    page.get_by_role("button", name="Delete older content", exact=True).click()
    expect(page.locator("#modal")).not_to_be_visible()
    expect(panel.get_by_role("button", name="Delete older responses", exact=True)).to_be_disabled()
    assert client.get(f"/api/runs/{stored_history['runs'][3]}").json()["responses"]

    page.locator('[data-storage-days="images"]').select_option("30")
    panel.get_by_role("button", name="Delete older images", exact=True).click()
    expect(page.locator("#modal")).to_contain_text(
        "Images shared with newer snapshots will be kept"
    )
    page.get_by_role("button", name="Delete older content", exact=True).click()
    expect(page.locator("#modal")).not_to_be_visible()
    assert client.get(f"/api/listing-assets/{asset_id('image-10')}").status_code == 200
    page.locator('[data-storage-days="images"]').select_option("7")
    panel.get_by_role("button", name="Delete older images", exact=True).click()
    page.get_by_role("button", name="Delete older content", exact=True).click()
    expect(page.locator("#modal")).not_to_be_visible()
    expect(panel.get_by_role("button", name="Delete older images", exact=True)).to_be_disabled()
    expect(panel).to_contain_text("0 B eligible for deletion")
    panel.screenshot(path=str(tmp_path / f"storage-cleaned-{width}.png"))

    page.goto(url + "/#activity")
    page.locator(f'[data-action="run"][data-id="{stored_history["runs"][10]}"]').click()
    expect(page.locator("#modal .storage-omission")).to_contain_text(
        "deleted during storage cleanup"
    )
    expect(page.locator("#modal .evidence")).to_contain_text("Matched app")
    page.get_by_role("button", name="Close dialog").click()
    page.goto(url + "/#listing-history")
    page.locator(
        f'[data-action="listing-snapshot"][data-id="{stored_history["snapshots"][10]}"]'
    ).click()
    expect(page.locator("#modal .storage-omission")).to_contain_text(
        "deleted during storage cleanup"
    )
    expect(page.locator("#modal .unavailable-image").first).to_have_text(
        "Image deleted during storage cleanup"
    )
    expect(page.locator("#modal .diff-text")).to_have_text(["Listing 40", "Listing 10"])
    page.screenshot(path=str(tmp_path / f"cleaned-history-{width}.png"))
