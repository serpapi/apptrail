from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import serpapi

from .regions import REGIONS

SOURCES = {
    "apple_app_store": {"label": "App Store", "kind": "store", "platform": "ios"},
    "google_play": {"label": "Google Play", "kind": "store", "platform": "android"},
    "google_ai_mode": {"label": "Google AI Mode", "kind": "ai"},
    "google_ai_overview": {"label": "Google AI Overview", "kind": "ai"},
    "bing_copilot": {"label": "Bing Copilot", "kind": "ai", "global": True},
}
ACCOUNT_FIELDS = {
    "account_status",
    "plan_name",
    "plan_renewal_date",
    "searches_per_month",
    "plan_searches_left",
    "extra_credits",
    "total_searches_left",
    "this_month_usage",
    "this_hour_searches",
    "account_rate_limit_per_hour",
}


def apple_language(language, country):
    available = REGIONS["apple_languages"]
    if language in available:
        return language
    regional = f"{language}-{country}"
    if regional in available:
        return regional
    preferred = {"en": "en-us", "pt": "pt-pt", "zh": "zh-cn"}.get(
        language, f"{language}-{language}"
    )
    if preferred in available:
        return preferred
    matches = sorted(code for code in available if code.split("-")[0] == language)
    if matches:
        return matches[0]
    raise ValueError("Choose a supported App Store language.")


def sanitize(value, secret=""):
    if isinstance(value, dict):
        return {
            k: sanitize(v, secret)
            for k, v in value.items()
            if k.lower() not in {"api_key", "authorization", "account_email"}
        }
    if isinstance(value, list):
        return [sanitize(v, secret) for v in value]
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, "[REDACTED]")
        return re.sub(r"(?i)(api_key=)[^&\s\"']+", r"\1[REDACTED]", value)
    return value


def http_url(value):
    value = str(value or "")
    parsed = urlparse(value)
    return (
        value
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
        else ""
    )


def store_identity(url):
    parsed = urlparse(str(url or ""))
    if parsed.hostname == "apps.apple.com":
        match = re.search(r"/id(\d+)(?:/|$)", parsed.path) or re.search(
            r"/app/(\d+)/?$", parsed.path
        )
        if match:
            return "ios", match.group(1)
    if parsed.hostname == "play.google.com" and parsed.path.rstrip("/") == "/store/apps/details":
        identifier = parse_qs(parsed.query).get("id", [""])[0]
        if re.fullmatch(r"[\w]+(?:\.[\w]+)+", identifier):
            return "android", identifier
    return None


def canonical_url(platform, identifier, country="us"):
    if platform == "ios":
        return f"https://apps.apple.com/{country}/app/id{identifier}"
    return f"https://play.google.com/store/apps/details?id={identifier}"


class ProviderError(Exception):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class SearchOutcome:
    result: dict
    responses: list = field(default_factory=list)
    requests_count: int = 0


def profile_stats(data, platform):
    info = data.get("product_info", data)
    rating = info.get("rating")
    if isinstance(rating, list):
        rating = (rating[0] if rating else {}).get("rating")
    count = info.get("reviews") or info.get("rating_count")
    if not count and isinstance(data.get("rating"), list):
        count = (data["rating"][0] if data["rating"] else {}).get("count")
    return {
        "rating": rating,
        "reviews": count,
        "downloads": info.get("downloads"),
        "version": data.get("version")
        or data.get("information", {}).get("version")
        or next(
            (
                entry.get("release_version") or entry.get("version")
                for entry in data.get("version_history", [])
                if isinstance(entry, dict)
            ),
            None,
        ),
        "price": info.get("price"),
        "platform": platform,
    }


def normalize_listing(item, platform, country="us", language="en", identifier=None):
    info = item.get("product_info", item)
    identity = store_identity(item.get("link", ""))
    identifier = str(
        identifier
        or item.get("id" if platform == "ios" else "product_id")
        or (identity[1] if identity else "")
    )
    if not identifier or not info.get("title"):
        return None
    developer = item.get("developer", {})
    if isinstance(developer, dict):
        developer = developer.get("name", "")
    authors = info.get("authors") or []
    icon = (
        info.get("thumbnail")
        or item.get("thumbnail")
        or item.get("icon")
        or item.get("logo")
        or next((logo.get("link") for logo in item.get("logos", []) if isinstance(logo, dict)), "")
    )
    return {
        "platform": platform,
        "external_id": identifier,
        "bundle_id": item.get("bundle_id", ""),
        "title": info["title"],
        "url": canonical_url(platform, identifier, country),
        "icon": http_url(icon),
        "developer": developer
        or item.get("author")
        or (authors[0].get("name", "") if authors else ""),
        "country": country,
        "language": language,
        "metadata_json": profile_stats(item, platform),
    }


def store_results(data, platform, offset=0):
    results = []
    for section_index, section in enumerate(data.get("organic_results") or []):
        if not isinstance(section, dict):
            continue
        if platform == "ios":
            items = [section]
            section_name = "Search results"
        else:
            items = section.get("items", [section])
            section_name = section.get("title") or "Search results"
        for i, item in enumerate(items):
            listing = normalize_listing(item, platform)
            if listing:
                listing.update(
                    position=item.get("position")
                    or (section_index + 1 if platform == "ios" else i + 1) + offset,
                    section=section_name,
                    primary=section_index == 0 or platform == "ios",
                )
                results.append(listing)
    for key in ("app_highlight", "product_result"):
        highlighted = data.get(key)
        if isinstance(highlighted, dict):
            listing = normalize_listing(highlighted, platform)
            if listing:
                listing.update(position=None, section="Featured app", primary=False)
                results.append(listing)
    return results


def answer_content(data):
    texts, links = [], []

    def walk(node):
        if isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, dict):
            for key, value in node.items():
                if key in {"snippet", "text", "header", "title"} and isinstance(value, str):
                    texts.append(value)
                elif key in {"link", "url"} and http_url(value):
                    links.append(
                        {"title": node.get("text") or node.get("title", "Source"), "link": value}
                    )
                elif isinstance(value, (list, dict)) and key not in {
                    "references",
                    "reference_indexes",
                    "snippet_highlighted_words",
                }:
                    if key in {"table", "headers"}:
                        for row in value:
                            texts.append(
                                " | ".join(map(str, row)) if isinstance(row, list) else str(row)
                            )
                    else:
                        walk(value)

    if data.get("header"):
        texts.append(str(data["header"]))
    walk(data.get("text_blocks", []))
    for reference in data.get("references", []):
        if http_url(reference.get("link")):
            links.append({"title": reference.get("title", "Source"), "link": reference["link"]})
    unique = {link["link"]: link for link in links}
    return "\n\n".join(dict.fromkeys(texts)), list(unique.values())


class Gateway:
    def __init__(self, config):
        self.config = config
        self.responses = []
        self.requests_count = 0

    def client(self, key=None):
        key = self.config.api_key if key is None else key
        if not key:
            raise ProviderError("Connect a SerpApi key in Settings before searching.")
        return serpapi.Client(api_key=key, timeout=90)

    def account(self, key=None):
        key = self.config.api_key if key is None else key
        try:
            result = self.client(key).account()
        except Exception as exc:
            raise ProviderError(sanitize(getattr(exc, "error", None) or str(exc), key)) from None
        if result.get("error"):
            raise ProviderError(sanitize(str(result["error"]), key))
        return {k: result.get(k) for k in ACCOUNT_FIELDS}

    def request(self, **params):
        key = self.config.api_key
        self.requests_count += 1
        try:
            data = dict(self.client(key).search(**params))
        except Exception as exc:
            code = getattr(exc, "status_code", None)
            raise ProviderError(
                sanitize(getattr(exc, "error", None) or str(exc), key),
                retryable=code in {429, 500, 502, 503, 504} or code is None,
            ) from None
        data = sanitize(data, key)
        self.responses.append({"params": params, "data": data})
        status = data.get("search_metadata", {}).get("status")
        if status not in {None, "Success"}:
            raise ProviderError(
                str(data.get("error") or f"Search returned {status}"),
                retryable=status == "Processing",
            )
        if data.get("error") and not (
            status == "Success" and "hasn't returned any results" in data["error"]
        ):
            raise ProviderError(str(data["error"]))
        return data

    def discover(self, platform, query, country, language):
        identity = store_identity(query)
        if query.startswith(("http://", "https://")) and not identity:
            raise ProviderError("Use an apps.apple.com or play.google.com app listing URL.")
        identifier = identity[1] if identity else None
        if identity and identity[0] != platform:
            raise ProviderError("This link belongs to the other app store.")
        if platform == "ios" and query.isdigit():
            identifier = query
        if platform == "android" and re.fullmatch(r"\w+(?:\.\w+){2,}", query):
            identifier = query
        if identifier:
            return [self.product(platform, identifier, country, language)]
        params = {
            "engine": "apple_app_store" if platform == "ios" else "google_play",
            "term" if platform == "ios" else "q": query,
        }
        params.update(
            {
                "country": country,
                "lang": apple_language(language, country),
                "num": 20,
                "device": "mobile",
            }
            if platform == "ios"
            else {"gl": country, "hl": language, "store": "apps"}
        )
        data = self.request(**params)
        unique = {}
        for listing in sorted(
            store_results(data, platform), key=lambda item: item["section"] != "Featured app"
        ):
            listing = {
                key: value
                for key, value in listing.items()
                if key not in {"position", "section", "primary"}
            }
            listing.update(
                country=country,
                language=language,
                url=canonical_url(platform, listing["external_id"], country),
            )
            unique.setdefault(listing["external_id"], listing)
        return list(unique.values())[:12]

    def product(self, platform, identifier, country, language):
        params = {
            "engine": "apple_product" if platform == "ios" else "google_play_product",
            "product_id": identifier,
        }
        params.update(
            {"country": country, "type": "app"}
            if platform == "ios"
            else {"gl": country, "hl": language, "store": "apps"}
        )
        for refresh in (False, True):
            if refresh:
                # An incomplete response may be cached; retry once with a fresh lookup.
                params["no_cache"] = True
            data = self.request(**params)
            returned_id = data.get("id") or data.get("product_id")
            if returned_id and str(returned_id) != identifier:
                raise ProviderError("The store returned a different app identifier.")
            listing = normalize_listing(data, platform, country, language, identifier)
            if listing:
                return listing
        store = "App Store" if platform == "ios" else "Google Play"
        raise ProviderError(
            f"The {store} listing for {identifier} could not be verified in {country.upper()}. "
            "Please try again."
        )

    def search(self, spec):
        source = spec["source"]
        params = {"engine": source, "q": spec["query"]}
        if source == "apple_app_store":
            params["term"] = params.pop("q")
            params.update(
                country=spec["country"],
                lang=apple_language(spec["language"], spec["country"]),
                num=min(200, spec["depth"] * 50),
                device=spec.get("device") or "mobile",
            )
        elif source == "google_play":
            params.update(gl=spec["country"], hl=spec["language"], store="apps")
        elif source != "bing_copilot":
            params.update(
                gl=spec["country"], hl=spec["language"], device=spec.get("device") or "desktop"
            )
        if source == "google_ai_overview":
            params["engine"] = "google"
        data = self.request(**params)
        if SOURCES[source]["kind"] == "store":
            platform = SOURCES[source]["platform"]
            items = store_results(data, platform)
            pages = 1
            while source == "google_play" and pages < spec["depth"]:
                token = data.get("serpapi_pagination", {}).get("next_page_token")
                if not token:
                    break
                data = self.request(**params, next_page_token=token)
                items.extend(
                    store_results(data, platform, offset=len([x for x in items if x["primary"]]))
                )
                pages += 1
            result = {
                "kind": "store",
                "items": items,
                "results_checked": len(items),
                "pages": pages,
                "requested_depth": spec["depth"],
                "answer_available": None,
            }
        else:
            if source == "google_ai_overview":
                overview = data.get("ai_overview") or {}
                if overview.get("page_token"):
                    followup = self.request(
                        engine="google_ai_overview", page_token=overview["page_token"]
                    )
                    overview = followup.get("ai_overview", followup)
                if overview.get("error"):
                    raise ProviderError(str(overview["error"]))
                data = overview
            text, references = answer_content(data)
            result = {
                "kind": "ai",
                "text": text,
                "references": references,
                "answer_available": bool(text),
                "items": [],
            }
        return SearchOutcome(result, self.responses, self.requests_count)
