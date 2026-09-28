"""Opt-in listing collection and immutable local creative snapshots."""

import hashlib
import struct
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image, ImageOps
from sqlalchemy import select

from .db import App, Listing, ListingAsset, ListingSnapshot, ListingWatch, Run, now

MEDIA_FIELDS = {
    "icon",
    "screenshots",
    "iphone_screenshots",
    "ipad_screenshots",
    "watch_screenshots",
}


def snapshot_content(product, raw):
    info = raw.get("product_info", raw)
    history = raw.get("version_history") or []
    latest = history[0] if history else {}
    about = raw.get("about_this_app") or {}
    price = info.get("price", product.get("metadata_json", {}).get("price"))
    if price is None and product["platform"] == "android":
        offers = info.get("offers") or []
        if offers:
            price = offers[0].get("price") or offers[0].get("text")
            if price == "Install":
                price = "Free"
    result = {
        "title": product["title"],
        "subtitle": raw.get("short_description") or raw.get("snippet"),
        "description": raw.get("description") or about.get("description") or about.get("snippet"),
        "version": latest.get("release_version")
        or latest.get("version")
        or raw.get("version")
        or about.get("version"),
        "release_notes": latest.get("release_notes") or raw.get("what_s_new"),
        "price": price,
        "developer": product.get("developer"),
        "icon": product.get("icon") or None,
    }
    if product["platform"] == "ios":
        for field in ("iphone_screenshots", "ipad_screenshots", "watch_screenshots"):
            result[field] = raw.get(field, [])
        result["screenshots"] = raw.get("screenshots", [])
    else:
        result["screenshots"] = raw.get("media", {}).get("images", [])
    for field in MEDIA_FIELDS - {"icon"}:
        if field in result:
            result[field] = [
                item if isinstance(item, str) else item.get("link") or item.get("url")
                for item in result[field]
            ]
            result[field] = [url for url in result[field] if url][:30]
    # Structured provider fields remain readable in text comparisons.
    for field, value in list(result.items()):
        if field not in MEDIA_FIELDS and isinstance(value, (dict, list)):
            if isinstance(value, dict):
                result[field] = value.get("text") or value.get("snippet") or str(value)
            else:
                result[field] = "\n".join(str(item) for item in value)
    return result


def allowed_asset(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and any(
                host == domain or host.endswith("." + domain)
                for domain in ("mzstatic.com", "googleusercontent.com", "ggpht.com")
            )
        )
    except ValueError:
        return False


class AssetRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_asset(newurl):
            raise ValueError("Unsupported asset host.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_asset(url):
    if not allowed_asset(url):
        return None
    try:
        with build_opener(AssetRedirects()).open(
            Request(url, headers={"User-Agent": "AppTrail/0.1"}), timeout=8
        ) as response:
            content = response.read(4 * 1024 * 1024 + 1)
            if len(content) > 4 * 1024 * 1024:
                return None
            mime = (
                "image/png"
                if content.startswith(b"\x89PNG\r\n\x1a\n")
                else "image/jpeg"
                if content.startswith(b"\xff\xd8\xff")
                else "image/webp"
                if content.startswith(b"RIFF") and content[8:12] == b"WEBP"
                else "image/gif"
                if content.startswith((b"GIF87a", b"GIF89a"))
                else None
            )
            if not mime:
                return None
            return {"id": hashlib.sha256(content).hexdigest(), "mime": mime, "content": content}
    except Exception:
        return None


def image_pixel_hash(content):
    try:
        with Image.open(BytesIO(content)) as image:
            # Bound decoding work; animated assets retain their complete file identity.
            if image.width * image.height > 16_000_000 or getattr(image, "is_animated", False):
                return None
            pixels = ImageOps.exif_transpose(image).convert("RGBA")
            # RGB values under fully transparent pixels do not affect the displayed image.
            pixels = Image.alpha_composite(Image.new("RGBA", pixels.size), pixels)
            digest = hashlib.sha256(b"rgba-v1\0" + struct.pack(">II", *pixels.size))
            digest.update(pixels.tobytes())
            # Color profiles affect rendering, unlike comments and other file metadata.
            digest.update(image.info.get("icc_profile") or b"")
            return digest.hexdigest()
    except Exception:
        # An unsupported or damaged image can still be compared by its archived file hash.
        return None


def archive_media(data):
    urls = []
    for field in MEDIA_FIELDS:
        value = data.get(field)
        urls.extend(([value] if field == "icon" else value) or [])
    urls = list(dict.fromkeys(urls))[:40]
    with ThreadPoolExecutor(max_workers=4) as pool:
        archived = dict(zip(urls, pool.map(fetch_asset, urls), strict=True))
    assets = [asset for asset in archived.values() if asset]

    def reference(url):
        asset = archived.get(url)
        return {
            "url": url,
            "asset_id": asset["id"] if asset else None,
            "pixel_hash": image_pixel_hash(asset["content"]) if asset else None,
        }

    content = dict(data)
    for field in MEDIA_FIELDS:
        if field in content:
            content[field] = (
                reference(data[field])
                if field == "icon" and data[field]
                else [reference(url) for url in data[field]]
                if field != "icon"
                else None
            )
    return content, assets


def same_field(before, after):
    if (
        isinstance(before, dict)
        and isinstance(after, dict)
        and "asset_id" in before
        and "asset_id" in after
    ):
        # A failed image download is not evidence that a creative changed.
        if before.get("asset_id") and after.get("asset_id"):
            if before.get("pixel_hash") and after.get("pixel_hash"):
                return before["pixel_hash"] == after["pixel_hash"]
            return before["asset_id"] == after["asset_id"]
        return before.get("url") == after.get("url")
    if isinstance(before, list) and isinstance(after, list):
        return len(before) == len(after) and all(
            same_field(a, b) for a, b in zip(before, after, strict=True)
        )
    return before == after


def changed_fields(before, after):
    return [key for key in after if not same_field(before.get(key), after.get(key))]


def upgrade_image_comparisons(connection):
    pixel_hashes = {}
    previous_watch, previous_data = None, None
    snapshots = ListingSnapshot.__table__
    for snapshot in connection.execute(
        select(snapshots).order_by(snapshots.c.watch_id, snapshots.c.id)
    ).mappings():
        data = snapshot["data"]
        for field in MEDIA_FIELDS:
            refs = [data.get(field)] if field == "icon" else data.get(field) or []
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                asset_id = ref.get("asset_id")
                if asset_id and asset_id not in pixel_hashes:
                    content = connection.scalar(
                        select(ListingAsset.content).where(ListingAsset.id == asset_id)
                    )
                    pixel_hashes[asset_id] = image_pixel_hash(content) if content else None
                ref["pixel_hash"] = pixel_hashes.get(asset_id)
        changes = (
            changed_fields(previous_data, data)
            if previous_watch == snapshot["watch_id"] and not snapshot["baseline"]
            else []
        )
        connection.execute(
            snapshots.update()
            .where(snapshots.c.id == snapshot["id"])
            .values(data=data, changes=changes)
        )
        previous_watch, previous_data = snapshot["watch_id"], data


class ListingHistory:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def enqueue(self, session, watch, *, resume=False):
        existing = session.scalar(
            select(Run).where(Run.watch_id == watch.id, Run.status.in_(["queued", "running"]))
        )
        if existing:
            if resume and existing.params.get("cancel_requested"):
                existing.params = {
                    key: value
                    for key, value in existing.params.items()
                    if key != "cancel_requested"
                }
            return existing
        listing = session.get(Listing, watch.listing_id)
        run = Run(
            kind="listing_history",
            watch_id=watch.id,
            params={
                "app_id": listing.app_id,
                "platform": listing.platform,
                "external_id": listing.external_id,
                "country": watch.country,
                "language": watch.language,
                "source": "apple_app_store" if listing.platform == "ios" else "google_play",
            },
        )
        session.add(run)
        return run

    def dispatch(self, session):
        for watch in session.scalars(
            select(ListingWatch)
            .join(Listing)
            .join(App)
            .where(
                ListingWatch.enabled.is_(True),
                ListingWatch.next_run_at <= now(),
                App.archived.is_(False),
            )
        ):
            self.enqueue(session, watch)

    def save(self, session, run, content, assets):
        for asset in assets:
            if not session.get(ListingAsset, asset["id"]):
                session.add(ListingAsset(**asset))
                session.flush()
        previous = session.scalar(
            select(ListingSnapshot)
            .where(ListingSnapshot.watch_id == run.watch_id)
            .order_by(ListingSnapshot.id.desc())
            .limit(1)
        )
        session.add(
            ListingSnapshot(
                watch_id=run.watch_id,
                run_id=run.id,
                checked_at=run.started_at,
                data=content,
                baseline=previous is None,
                changes=changed_fields(previous.data, content) if previous else [],
            )
        )

    def watches(self):
        from .service import FREQUENCIES, record

        with self.db.session() as session:
            output = []
            for watch, listing, app in session.execute(
                select(ListingWatch, Listing, App)
                .join(Listing, Listing.id == ListingWatch.listing_id)
                .join(App, App.id == Listing.app_id)
                .order_by(ListingWatch.created_at)
            ):
                latest = session.scalar(
                    select(Run).where(Run.watch_id == watch.id).order_by(Run.id.desc()).limit(1)
                )
                output.append(
                    {
                        **record(watch),
                        "app_id": app.id,
                        "app_name": app.name,
                        "archived": app.archived,
                        "platform": listing.platform,
                        "external_id": listing.external_id,
                        "monthly_credits": round(30 / FREQUENCIES[watch.frequency], 1)
                        if watch.enabled and not app.archived
                        else 0,
                        "status": latest.status if latest else None,
                        "checked_at": latest.started_at if latest else None,
                        "error": latest.error if latest else "",
                    }
                )
            return output

    def timeline(self, watch_id, include_unchanged=False, before_id=None):
        from .service import record

        with self.db.session() as session:
            if not session.get(ListingWatch, watch_id):
                raise ValueError("Listing tracking not found.")
            query = select(ListingSnapshot).where(ListingSnapshot.watch_id == watch_id)
            if not include_unchanged:
                query = query.where(
                    (ListingSnapshot.baseline.is_(True)) | (ListingSnapshot.changes != [])
                )
            if before_id:
                query = query.where(ListingSnapshot.id < before_id)
            snapshots = list(session.scalars(query.order_by(ListingSnapshot.id.desc()).limit(51)))
            return {
                "snapshots": [{**record(item, exclude={"data"})} for item in snapshots[:50]],
                "next_cursor": snapshots[49].id if len(snapshots) > 50 else None,
            }

    def comparison(self, snapshot_id):
        from .service import record

        with self.db.session() as session:
            snapshot = session.get(ListingSnapshot, snapshot_id)
            if snapshot is None:
                raise ValueError("Snapshot not found.")
            previous = session.scalar(
                select(ListingSnapshot)
                .where(
                    ListingSnapshot.watch_id == snapshot.watch_id, ListingSnapshot.id < snapshot.id
                )
                .order_by(ListingSnapshot.id.desc())
                .limit(1)
            )
            return {
                "snapshot": record(snapshot),
                "previous": record(previous) if previous else None,
            }
