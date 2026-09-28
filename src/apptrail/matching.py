import re
import unicodedata
from urllib.parse import urlparse

from .engines import store_identity


def normalized(value):
    return unicodedata.normalize("NFKC", value).casefold()


def source_offset(text, offset):
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(normalized(text[:middle])) <= offset:
            low = middle
        else:
            high = middle - 1
    return low


def match_app(app, listings, result):
    identities = {(listing.platform, listing.external_id) for listing in listings}
    evidence = []
    if result["kind"] == "store":
        hits = [
            item
            for item in result["items"]
            if (item["platform"], item["external_id"]) in identities
        ]
        ranked = [item for item in hits if item.get("primary") and item.get("position")]
        best = (
            min(ranked, key=lambda item: item["position"])
            if ranked
            else (hits[0] if hits else None)
        )
        if best:
            evidence.append({"type": "identifier", "text": best["external_id"], "url": best["url"]})
        return {
            "found": bool(hits),
            "position": best.get("position") if best and best.get("primary") else None,
            "section": best.get("section") if best else None,
            "evidence": evidence,
            "results_checked": result["results_checked"],
            "mentioned": False,
            "cited": False,
            "possible": False,
        }
    text = result.get("text", "")
    search_text = normalized(text)
    names = list(dict.fromkeys([app.name, *app.aliases, *(item.title for item in listings)]))
    mentioned, possible = False, False
    for name in names:
        match = re.search(r"(?<!\w)" + re.escape(normalized(name)) + r"(?!\w)", search_text)
        if match:
            ambiguous = len(name.strip()) < 4 or normalized(name) in {
                "notes",
                "calendar",
                "mail",
                "music",
                "weather",
                "files",
            }
            mentioned |= not ambiguous
            possible |= ambiguous
            evidence.append(
                {
                    "type": "possible_name" if ambiguous else "name",
                    "text": text[
                        max(0, source_offset(text, match.start()) - 70) : source_offset(
                            text, match.end()
                        )
                        + 100
                    ],
                }
            )
    exact, website = False, False
    app_url = urlparse(app.website)
    app_host = (app_url.hostname or "").removeprefix("www.")
    for reference in result.get("references", []):
        link = reference["link"]
        if store_identity(link) in identities:
            exact = True
            evidence.append(
                {"type": "app_link", "text": reference.get("title", "App listing"), "url": link}
            )
        parsed = urlparse(link)
        # Require both the configured domain and app-specific path, if supplied.
        if (
            app_host
            and (parsed.hostname or "").removeprefix("www.") == app_host
            and (
                app_url.path.rstrip("/") in {"", "/"}
                or parsed.path.rstrip("/") == app_url.path.rstrip("/")
                or parsed.path.startswith(app_url.path.rstrip("/") + "/")
            )
        ):
            website = True
            evidence.append(
                {"type": "website", "text": reference.get("title", "Website"), "url": link}
            )
    return {
        "found": mentioned or exact,
        "mentioned": mentioned,
        "cited": exact or website,
        "app_link": exact,
        "website_cited": website,
        "possible": possible and not (mentioned or exact),
        "position": None,
        "answer_available": result.get("answer_available", False),
        "evidence": evidence,
    }
