import copy
import hashlib
import json
import sqlite3
from io import BytesIO
from types import SimpleNamespace

import pytest
from conftest import FakeGateway
from PIL import Image, PngImagePlugin
from test_insights import watch

from apptrail.db import Database
from apptrail.listing_history import (
    ListingHistory,
    archive_media,
    changed_fields,
    image_pixel_hash,
)


def image_bytes(*, format="WEBP", metadata=False, changed=False):
    image = Image.new("RGBA", (12, 18), "#de483a")
    if changed:
        image.putpixel((5, 8), (0, 0, 255, 255))
    output = BytesIO()
    options = {}
    if format == "WEBP":
        options["lossless"] = True
        if metadata:
            options["xmp"] = b'<x:xmpmeta xmlns:x="adobe:ns:meta/">Comment</x:xmpmeta>'
    elif metadata:
        options["pnginfo"] = PngImagePlugin.PngInfo()
        options["pnginfo"].add_text("Comment", "Different metadata, same pixels")
    image.save(output, format=format, **options)
    return output.getvalue()


def asset(content):
    return {"id": hashlib.sha256(content).hexdigest(), "mime": "image/webp", "content": content}


@pytest.mark.parametrize("format", ["WEBP", "PNG"])
def test_metadata_changes_keep_original_archives_without_listing_changes(monkeypatch, format):
    old, new = image_bytes(format=format), image_bytes(format=format, metadata=True)
    assert old != new
    assert Image.open(BytesIO(old)).tobytes() == Image.open(BytesIO(new)).tobytes()
    media = {
        "icon": "https://play-lh.googleusercontent.com/icon",
        "screenshots": ["https://play-lh.googleusercontent.com/screenshot"],
    }
    monkeypatch.setattr("apptrail.listing_history.fetch_asset", lambda url: asset(old))
    before, old_assets = archive_media(media)
    monkeypatch.setattr("apptrail.listing_history.fetch_asset", lambda url: asset(new))
    after, new_assets = archive_media(media)
    assert before["icon"]["asset_id"] != after["icon"]["asset_id"]
    assert before["icon"]["pixel_hash"] == after["icon"]["pixel_hash"]
    assert changed_fields(before, after) == []
    assert old_assets[0]["content"] == old and new_assets[0]["content"] == new


def test_pixel_identity_preserves_orientation_and_ignores_lossless_encoding():
    plain = image_bytes(format="PNG", changed=True)
    assert image_pixel_hash(plain) == image_pixel_hash(image_bytes(changed=True))
    source = Image.open(BytesIO(plain))
    output = BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    source.transpose(Image.Transpose.ROTATE_90).save(output, format="PNG", exif=exif)
    assert image_pixel_hash(plain) == image_pixel_hash(output.getvalue())
    assert image_pixel_hash(plain) != image_pixel_hash(image_bytes())


def test_transparent_rgb_is_ignored_but_color_profiles_are_preserved():
    images = []
    for color in [(255, 0, 0, 0), (0, 255, 0, 0)]:
        output = BytesIO()
        Image.new("RGBA", (2, 3), color).save(output, format="PNG")
        images.append(output.getvalue())
    assert image_pixel_hash(images[0]) == image_pixel_hash(images[1])
    output = BytesIO()
    Image.open(BytesIO(images[0])).save(output, format="PNG", icc_profile=b"different profile")
    assert image_pixel_hash(images[0]) != image_pixel_hash(output.getvalue())


def test_animated_and_invalid_assets_fall_back_to_complete_file_identity(monkeypatch):
    frames = [Image.new("RGB", (5, 5), color) for color in ("red", "blue")]
    output = BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=100)
    assert image_pixel_hash(output.getvalue()) is None
    assert image_pixel_hash(b"invalid image") is None
    media = {"icon": "https://play-lh.googleusercontent.com/icon"}
    monkeypatch.setattr("apptrail.listing_history.fetch_asset", lambda url: asset(b"invalid image"))
    before, _ = archive_media(media)
    monkeypatch.setattr(
        "apptrail.listing_history.fetch_asset", lambda url: asset(output.getvalue())
    )
    after, _ = archive_media(media)
    assert changed_fields(before, after) == ["icon"]
    monkeypatch.setattr("apptrail.listing_history.fetch_asset", lambda url: None)
    unavailable, _ = archive_media(media)
    assert changed_fields(before, unavailable) == []


@pytest.fixture
def listing_images(monkeypatch):
    current = {"content": image_bytes(), "screenshots": ["one", "two"]}
    original = FakeGateway.product

    def product(self, *args):
        value = original(self, *args)
        value["icon"] = "https://play-lh.googleusercontent.com/icon"
        self.responses = [
            {
                "data": {
                    "media": {
                        "images": [
                            "https://play-lh.googleusercontent.com/" + name
                            for name in current["screenshots"]
                        ]
                    }
                }
            }
        ]
        return value

    def fetch(url):
        return asset(image_bytes(changed=True) if url.endswith("two") else current["content"])

    monkeypatch.setattr(FakeGateway, "product", product)
    monkeypatch.setattr("apptrail.listing_history.fetch_asset", fetch)
    return current


def collect(client, watch_id, *, first=False):
    if not first:
        assert client.post(f"/api/listing-watches/{watch_id}/check").status_code == 200
    assert client.app.state.worker.process_one()
    return client.get(f"/api/listing-watches/{watch_id}/history?include_unchanged=true").json()[
        "snapshots"
    ][0]


def test_history_ignores_metadata_but_detects_pixel_and_screenshot_changes(
    client, tracked, listing_images
):
    watch_id = watch(client, platform="android")
    baseline = collect(client, watch_id, first=True)
    listing_images["content"] = image_bytes(metadata=True)
    unchanged = collect(client, watch_id)
    assert unchanged["changes"] == []
    assert [
        s["id"] for s in client.get(f"/api/listing-watches/{watch_id}/history").json()["snapshots"]
    ] == [baseline["id"]]
    comparison = client.get(f"/api/listing-snapshots/{unchanged['id']}").json()
    for snapshot, content in [
        (comparison["previous"], image_bytes()),
        (comparison["snapshot"], image_bytes(metadata=True)),
    ]:
        icon = snapshot["data"]["icon"]
        assert client.get(f"/api/listing-assets/{icon['asset_id']}").content == content
    listing_images["screenshots"] = ["two", "one"]
    assert collect(client, watch_id)["changes"] == ["screenshots"]
    listing_images["screenshots"].append("three")
    assert collect(client, watch_id)["changes"] == ["screenshots"]
    listing_images["screenshots"].pop()
    assert collect(client, watch_id)["changes"] == ["screenshots"]
    listing_images["content"] = image_bytes(changed=True)
    assert set(collect(client, watch_id)["changes"]) == {"icon", "screenshots"}


def test_upgrade_corrects_saved_history_and_keeps_originals_and_backup(
    client, tracked, listing_images, tmp_path
):
    watch_id = watch(client, platform="android")
    baseline = collect(client, watch_id, first=True)
    listing_images["content"] = image_bytes(metadata=True)
    unchanged = collect(client, watch_id)
    listing_images["content"] = image_bytes(changed=True)
    changed = collect(client, watch_id)
    other_watch = watch(client, platform="android", country="gb")
    other_baseline = collect(client, other_watch, first=True)

    directory = tmp_path / "legacy"
    directory.mkdir()
    client.app.state.service.db.backup(directory / "apptrail.sqlite3")
    with sqlite3.connect(directory / "apptrail.sqlite3") as connection:
        previous = {}
        originals = {}
        rows = connection.execute(
            "SELECT id,watch_id,data FROM listing_snapshots ORDER BY id"
        ).fetchall()
        for id, wid, raw in rows:
            data = json.loads(raw)
            for ref in [data["icon"], *data["screenshots"]]:
                ref.pop("pixel_hash")
            changes = changed_fields(previous[wid], data) if wid in previous else []
            connection.execute(
                "UPDATE listing_snapshots SET data=?,changes=? WHERE id=?",
                (json.dumps(data), json.dumps(changes), id),
            )
            originals[id] = copy.deepcopy(data)
            previous[wid] = data
        assert json.loads(
            connection.execute(
                "SELECT changes FROM listing_snapshots WHERE id=?", (unchanged["id"],)
            ).fetchone()[0]
        ) == ["icon", "screenshots"]
        original_assets = connection.execute(
            "SELECT id,content FROM listing_assets ORDER BY id"
        ).fetchall()
        connection.execute("PRAGMA user_version=6")

    upgraded = Database(directory)
    history = ListingHistory(SimpleNamespace(db=upgraded))
    assert [s["id"] for s in history.timeline(watch_id)["snapshots"]] == [
        changed["id"],
        baseline["id"],
    ]
    assert history.comparison(unchanged["id"])["snapshot"]["changes"] == []
    assert set(history.comparison(changed["id"])["snapshot"]["changes"]) == {"icon", "screenshots"}
    assert history.comparison(other_baseline["id"])["snapshot"]["changes"] == []
    for id, original in originals.items():
        data = history.comparison(id)["snapshot"]["data"]
        for ref in [data["icon"], *data["screenshots"]]:
            assert ref.pop("pixel_hash")
        assert data == original
    upgraded.close()
    with sqlite3.connect(directory / "apptrail.sqlite3") as connection:
        assert (
            connection.execute("SELECT id,content FROM listing_assets ORDER BY id").fetchall()
            == original_assets
        )
    with sqlite3.connect(directory / "backups/before-schema-6.sqlite3") as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 6
        assert json.loads(
            backup.execute(
                "SELECT changes FROM listing_snapshots WHERE id=?", (unchanged["id"],)
            ).fetchone()[0]
        ) == ["icon", "screenshots"]
    Database(directory).close()


@pytest.mark.parametrize("failure", ["missing", "invalid"])
def test_upgrade_tolerates_unavailable_archived_images(
    client, tracked, listing_images, tmp_path, failure
):
    if failure == "invalid":
        listing_images["content"] = b"undecodable saved image"
    watch_id = watch(client, platform="android")
    baseline = collect(client, watch_id, first=True)
    directory = tmp_path / "legacy"
    directory.mkdir()
    client.app.state.service.db.backup(directory / "apptrail.sqlite3")
    with sqlite3.connect(directory / "apptrail.sqlite3") as connection:
        if failure == "missing":
            connection.execute("DELETE FROM listing_assets")
        connection.execute("PRAGMA user_version=6")
    upgraded = Database(directory)
    history = ListingHistory(SimpleNamespace(db=upgraded))
    result = history.comparison(baseline["id"])["snapshot"]
    assert result["data"]["icon"]["pixel_hash"] is None
    assert result["changes"] == [] and result["baseline"]
    upgraded.close()
