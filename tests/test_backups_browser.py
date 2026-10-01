import os

import pytest
from conftest import TEST_PASSWORD, add_monitor, sign_in
from playwright.sync_api import expect
from sqlalchemy import delete
from test_backups import restore
from test_insights import watch
from test_insights_browser import browser as browser  # noqa: F401
from test_insights_browser import browser_page as browser_page  # noqa: F401
from test_insights_browser import login
from test_listing_images import collect
from test_listing_images import listing_images as listing_images

from apptrail.auth import Auth
from apptrail.db import App, LoginSession, Owner

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.getenv("APPTRAIL_BROWSER_TESTS") != "1", reason="Opt-in local browser check"
    ),
]


@pytest.mark.parametrize("width", [1440, 390])
def test_settings_restore_warning_validation_and_logout(
    client, tracked, browser_page, tmp_path, width
):
    backup = tmp_path / "restore.sqlite3"
    backup.write_bytes(client.get("/api/backup").content)
    new_password = "current password after backup 3!"
    response = client.post(
        "/api/auth/password",
        json={"current_password": TEST_PASSWORD, "new_password": new_password},
    )
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    assert client.patch(f"/api/apps/{tracked}", json={"name": "New name"}).status_code == 200
    page, url = browser_page
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(url + "/login")
    page.get_by_role("textbox", name="Username").fill("owner")
    page.get_by_label("Password", exact=True).fill(new_password)
    page.locator("#auth-submit").click()
    page.wait_for_url(url + "/")
    page.goto(url + "/#settings")
    backup_panel = page.locator(".settings-panel").filter(
        has=page.get_by_role("heading", name="Data & backups")
    )
    expect(backup_panel).to_contain_text("CSV files cannot restore your workspace")
    expect(backup_panel).to_contain_text(
        "Raw SerpApi responses and listing-history images are omitted"
    )
    expect(backup_panel).to_contain_text("Login sessions and your SerpApi key are not included")
    expect(backup_panel.get_by_role("link", name="Download SQLite backup")).to_have_attribute(
        "href", "/api/backup"
    )
    page.get_by_role("button", name="Restore from SQLite", exact=True).click()
    expect(page.locator("#modal")).to_contain_text("This will overwrite all current server data")
    expect(page.locator("#modal")).to_contain_text("Everyone will be signed out")
    page.screenshot(path=str(tmp_path / f"restore-dialog-{width}.png"))
    assert page.locator("#modal").evaluate("el => el.scrollWidth <= el.clientWidth")
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#modal")).not_to_be_visible()
    assert client.get("/api/state").json()["apps"][0]["name"] == "New name"

    page.get_by_role("button", name="Restore from SQLite", exact=True).click()
    page.get_by_label("SQLite backup file").set_input_files(
        {"name": "bad.sqlite3", "mimeType": "application/vnd.sqlite3", "buffer": b"bad"}
    )
    page.get_by_role("button", name="Overwrite and restore").click()
    assert page.locator("#restore-backup-form").evaluate("el => !el.checkValidity()")
    page.get_by_role("checkbox").check()
    page.get_by_role("button", name="Overwrite and restore").click()
    expect(page.locator("#modal-error")).to_contain_text("Upload an AppTrail SQLite backup")
    expect(page.get_by_role("button", name="Overwrite and restore")).to_be_enabled()
    assert client.get("/api/state").json()["apps"][0]["name"] == "New name"

    page.get_by_label("SQLite backup file").set_input_files(backup)
    page.get_by_role("button", name="Overwrite and restore").click()
    page.wait_for_url(url + "/login?restored=1")
    expect(page.locator("#auth-description")).to_contain_text("Backup restored")
    expect(page.locator("#auth-description")).to_contain_text("password saved in that backup")
    assert client.get("/api/state").status_code == 401
    login(page, url)
    expect(page.locator("#main")).to_contain_text("Todo Example")


@pytest.mark.parametrize("width", [1440, 390])
def test_setup_restore_is_secondary_and_restores_without_creating_account(
    client,
    tracked,
    browser_page,
    tmp_path,
    width,
):
    backup = tmp_path / "setup-restore.sqlite3"
    backup.write_bytes(client.get("/api/backup").content)
    service = client.app.state.service
    with service.db.session.begin() as session:
        session.execute(delete(LoginSession))
        session.execute(delete(Owner))
        session.execute(delete(App))
    Auth(service.db, service.config)
    token = client.app.state.auth.setup_token()
    page, url = browser_page
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(url + "/login")
    expect(page.get_by_role("heading", name="Create your account")).to_be_visible()
    expect(page.locator("#setup-restore-form")).to_be_hidden()
    expect(page.get_by_role("button", name="Create account", exact=True)).to_be_visible()
    entry = page.get_by_role("button", name="Have a backup? Restore your data")
    expect(entry).to_be_visible()
    assert entry.evaluate("el => getComputedStyle(el).fontSize") == "12px"
    page.get_by_role("textbox", name="Setup code", exact=True).fill(token)
    page.screenshot(path=str(tmp_path / f"setup-{width}.png"), full_page=True)
    entry.click()
    expect(page.get_by_role("heading", name="Restore your data")).to_be_visible()
    expect(page.locator("#auth-description")).to_contain_text("after an update or move")
    expect(page.get_by_role("textbox", name="Setup code", exact=True)).to_have_value(token)
    page.get_by_role("button", name="Back to account setup").click()
    expect(page.get_by_role("button", name="Create account", exact=True)).to_be_visible()
    entry.click()
    page.get_by_label("SQLite backup file").set_input_files(backup)
    page.get_by_role("button", name="Restore backup", exact=True).click()
    assert page.locator("#setup-restore-form").evaluate("el => !el.checkValidity()")
    page.get_by_role("checkbox").check()
    page.get_by_role("textbox", name="Setup code", exact=True).fill("wrong")
    page.get_by_role("button", name="Restore backup", exact=True).click()
    expect(page.locator("#setup-restore-error")).to_contain_text("setup code is incorrect")
    page.get_by_role("textbox", name="Setup code", exact=True).fill(token)
    page.get_by_label("SQLite backup file").set_input_files(
        {
            "name": "bad.sqlite3",
            "mimeType": "application/vnd.sqlite3",
            "buffer": b"bad",
        }
    )
    page.get_by_role("button", name="Restore backup", exact=True).click()
    expect(page.locator("#setup-restore-error")).to_contain_text("Upload an AppTrail SQLite backup")
    assert client.get("/api/auth/status").json()["setup_required"]
    page.get_by_label("SQLite backup file").set_input_files(backup)
    page.screenshot(path=str(tmp_path / f"setup-restore-{width}.png"), full_page=True)
    page.get_by_role("button", name="Restore backup", exact=True).click()
    page.wait_for_url(url + "/login?restored=1")
    expect(page.locator("#auth-description")).to_contain_text("Backup restored")
    expect(entry).to_be_hidden()
    assert client.get("/api/auth/status").json()["setup_required"] is False
    login(page, url)
    expect(page.locator("#main")).to_contain_text("Todo Example")
    sign_in(client)
    assert client.get("/api/state").json()["apps"][0]["name"] == "Todo Example"


@pytest.mark.parametrize("width", [1440, 390])
def test_restored_backup_explains_omissions_without_broken_images(
    client,
    tracked,
    listing_images,
    browser_page,
    tmp_path,
    width,
):
    watch_id = watch(client, platform="android")
    collect(client, watch_id, first=True)
    monitor_id = add_monitor(client, tracked)
    assert client.app.state.worker.process_one()
    run_id = next(
        run["id"]
        for run in client.get("/api/dashboard").json()["runs"]
        if run["monitor_id"] == monitor_id
    )
    assert restore(client, client.get("/api/backup").content).status_code == 200
    sign_in(client)
    page, url = browser_page
    page.set_viewport_size({"width": width, "height": 1000})
    requested_images = []
    page.on(
        "request",
        lambda request: (
            requested_images.append(request.url) if "/api/listing-assets/" in request.url else None
        ),
    )
    login(page, url)
    page.goto(url + "/#listing-history")
    page.locator(".timeline-event").first.click()
    expect(page.locator("#modal .backup-omission")).to_contain_text(
        "images were omitted from this backup"
    )
    expect(page.locator("#modal .unavailable-image").first).to_have_text(
        "Image omitted from backup to save space"
    )
    expect(page.locator("#modal img")).to_have_count(0)
    assert requested_images == []
    page.screenshot(path=str(tmp_path / f"omitted-images-{width}.png"))
    page.get_by_role("button", name="Close dialog").click()
    page.goto(url + "/#activity")
    page.locator(f'[data-action="run"][data-id="{run_id}"]').click()
    expect(page.locator("#modal .backup-omission")).to_contain_text(
        "Original SerpApi responses were omitted"
    )
    expect(page.locator("#modal")).to_contain_text("Store results")
    expect(page.locator("#modal .evidence")).not_to_have_count(0)
    page.screenshot(path=str(tmp_path / f"omitted-responses-{width}.png"))
    page.get_by_role("button", name="Close dialog").click()

    # Subsequent collection can display images again and should not claim they were omitted.
    collect(client, watch_id)
    page.goto(url + "/#listing-history")
    page.locator(".timeline-event").first.click()
    expect(page.locator("#modal .backup-omission")).to_have_count(0)
    expect(page.locator("#modal .unavailable-image")).to_have_count(0)
    page.wait_for_function("""() => [...document.querySelectorAll('#modal img')]
        .some(img => img.complete && img.naturalWidth > 0)""")
