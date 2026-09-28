from types import SimpleNamespace

import pytest

from apptrail.engines import Gateway, ProviderError


class ScriptedGateway(Gateway):
    def __init__(self, responses):
        super().__init__(SimpleNamespace(api_key="test"))
        self.script = iter(responses)
        self.requested = []

    def request(self, **params):
        self.requested.append(params)
        return next(self.script)


def spec(source, **extra):
    return {
        "source": source,
        "query": "apps",
        "country": "us",
        "language": "en",
        "depth": 1,
        "device": "",
        **extra,
    }


def test_overview_followup_is_immediate_and_separate_from_google_organic():
    gateway = ScriptedGateway(
        [
            {
                "ai_overview": {"page_token": "short-lived"},
                "organic_results": [{"snippet": "Wrong text"}],
            },
            {"ai_overview": {"text_blocks": [{"snippet": "Actual answer"}], "references": []}},
        ]
    )
    result = gateway.search(spec("google_ai_overview")).result
    assert gateway.requested == [
        {"engine": "google", "q": "apps", "gl": "us", "hl": "en", "device": "desktop"},
        {"engine": "google_ai_overview", "page_token": "short-lived"},
    ]
    assert result["text"] == "Actual answer" and result["answer_available"]


def test_no_overview_is_successful_absence():
    gateway = ScriptedGateway([{"organic_results": [{"title": "Organic result"}]}])
    result = gateway.search(spec("google_ai_overview")).result
    assert not result["answer_available"] and result["text"] == ""


def test_play_pagination_keeps_section_positions():
    gateway = ScriptedGateway(
        [
            {
                "organic_results": [
                    {
                        "items": [
                            {"title": "A", "product_id": "a.b"},
                            {"title": "B", "product_id": "c.d"},
                        ]
                    }
                ],
                "serpapi_pagination": {"next_page_token": "next-page"},
            },
            {"organic_results": [{"items": [{"title": "C", "product_id": "e.f"}]}]},
        ]
    )
    result = gateway.search(spec("google_play", depth=3)).result
    assert result["pages"] == 2
    assert [item["position"] for item in result["items"]] == [1, 2, 3]
    assert gateway.requested[1]["next_page_token"] == "next-page"


def test_apple_request_uses_term_and_mobile():
    gateway = ScriptedGateway([{}])
    gateway.search(spec("apple_app_store", depth=3))
    params = gateway.requested[0]
    assert params["term"] == "apps" and params["num"] == 150
    assert params["lang"] == "en-us" and params["device"] == "mobile"
    assert "q" not in params


def test_bing_does_not_send_unsupported_locale():
    gateway = ScriptedGateway([{"header": "Answer"}])
    gateway.search(spec("bing_copilot"))
    assert gateway.requested == [{"engine": "bing_copilot", "q": "apps"}]


def test_apple_product_preserves_recorded_rating_and_latest_release():
    # Field structure comes from the saved apple_product SerpApi response.
    gateway = ScriptedGateway(
        [
            {
                "id": "572688855",
                "title": "Todoist: To Do List & Calendar",
                "developer": {"name": "Todoist Inc."},
                "rating": 4.8,
                "rating_count": 120000,
                "price": "Free",
                "version_history": [
                    {"release_version": "26.9.13", "release_date": "2026-09-21"},
                    {"release_version": "26.9.12", "release_date": "2026-09-15"},
                ],
            }
        ]
    )
    listing = gateway.product("ios", "572688855", "gb", "en")
    assert listing["external_id"] == "572688855"
    assert listing["developer"] == "Todoist Inc."
    assert listing["url"] == "https://apps.apple.com/gb/app/id572688855"
    assert listing["country"] == "gb"
    assert listing["metadata_json"] == {
        "rating": 4.8,
        "reviews": 120000,
        "downloads": None,
        "version": "26.9.13",
        "price": "Free",
        "platform": "ios",
    }


def test_play_product_uses_requested_identifier_and_nested_store_statistics():
    # Play product responses nest these fields and need not repeat the product ID.
    gateway = ScriptedGateway(
        [
            {
                "product_info": {
                    "title": "Todoist: To Do List & Calendar",
                    "authors": [{"name": "Todoist Inc."}],
                    "rating": 4.6,
                    "reviews": 305000,
                    "downloads": "10M+",
                    "thumbnail": "https://play-lh.googleusercontent.com/test-icon",
                    "offers": [{"text": "Install"}],
                }
            }
        ]
    )
    listing = gateway.product("android", "com.todoist", "in", "en")
    assert listing["external_id"] == "com.todoist"
    assert listing["title"] == "Todoist: To Do List & Calendar"
    assert listing["developer"] == "Todoist Inc."
    assert listing["url"] == "https://play.google.com/store/apps/details?id=com.todoist"
    assert listing["icon"] == "https://play-lh.googleusercontent.com/test-icon"
    assert listing["country"] == "in"
    assert listing["metadata_json"] == {
        "rating": 4.6,
        "reviews": 305000,
        "downloads": "10M+",
        "version": None,
        "price": None,
        "platform": "android",
    }


def test_product_rejects_different_identifier_even_when_title_matches():
    gateway = ScriptedGateway([{"id": "999", "title": "Todoist: To Do List & Calendar"}])
    with pytest.raises(ProviderError, match="different app identifier"):
        gateway.product("ios", "572688855", "us", "en")
    assert len(gateway.requested) == 1


@pytest.mark.parametrize("platform,identifier", [("ios", "572688855"), ("android", "com.todoist")])
def test_product_retries_incomplete_response_without_cache(monkeypatch, platform, identifier):
    info = {"title": "Todoist: To Do List & Calendar", "rating": 4.8}
    complete = info if platform == "ios" else {"product_info": info}
    responses = iter([{"search_metadata": {"status": "Success"}}, complete])
    gateway = Gateway(SimpleNamespace(api_key="test"))
    monkeypatch.setattr(
        gateway, "client", lambda key: SimpleNamespace(search=lambda **params: next(responses))
    )

    listing = gateway.product(platform, identifier, "gb", "en")

    assert listing["external_id"] == identifier
    assert listing["title"] == info["title"]
    assert listing["metadata_json"]["rating"] == 4.8
    assert listing["country"] == "gb"
    assert gateway.requests_count == 2
    original, retried = [response["params"] for response in gateway.responses]
    assert "no_cache" not in original
    assert retried == {**original, "no_cache": True}


@pytest.mark.parametrize(
    "platform,identifier,store",
    [("ios", "572688855", "App Store"), ("android", "com.todoist", "Google Play")],
)
def test_product_rejects_listing_still_incomplete_after_refresh(platform, identifier, store):
    gateway = ScriptedGateway([{}, {}])

    with pytest.raises(ProviderError) as caught:
        gateway.product(platform, identifier, "gb", "en")

    assert f"{store} listing for {identifier} could not be verified in GB" in str(caught.value)
    assert len(gateway.requested) == 2
    assert gateway.requested[1] == {**gateway.requested[0], "no_cache": True}


def test_product_refresh_still_rejects_different_identifier():
    gateway = ScriptedGateway([{}, {"id": "999", "title": "Todoist: To Do List & Calendar"}])
    with pytest.raises(ProviderError, match="different app identifier"):
        gateway.product("ios", "572688855", "us", "en")
    assert len(gateway.requested) == 2


def test_product_does_not_retry_explicit_provider_errors(monkeypatch):
    gateway = Gateway(SimpleNamespace(api_key="test"))
    monkeypatch.setattr(
        gateway,
        "client",
        lambda key: SimpleNamespace(
            search=lambda **params: {"search_metadata": {"status": "Error"}, "error": "Not found"}
        ),
    )
    with pytest.raises(ProviderError, match="Not found"):
        gateway.product("ios", "572688855", "us", "en")
    assert gateway.requests_count == 1


@pytest.mark.parametrize("operation", ["search", "search_error", "account_error"])
def test_key_rotation_during_request_does_not_expose_original_key(monkeypatch, operation):
    original_key = "original-secret-for-test-only"
    config = SimpleNamespace(api_key=original_key)

    class RotatingClient:
        def __init__(self, api_key, **kwargs):
            assert api_key == original_key

        def search(self, **params):
            config.api_key = "replacement-secret-for-test-only"
            if operation == "search_error":
                raise RuntimeError(f"Rejected credential {original_key}")
            return {"search_metadata": {"status": "Success"}, "message": original_key}

        def account(self):
            config.api_key = "replacement-secret-for-test-only"
            raise RuntimeError(f"Rejected credential {original_key}")

    monkeypatch.setattr("apptrail.engines.serpapi.Client", RotatingClient)
    gateway = Gateway(config)
    if operation == "search":
        result = gateway.request(engine="google", q="apps")
        assert original_key not in str(result)
        assert original_key not in str(gateway.responses)
    else:
        with pytest.raises(ProviderError) as caught:
            if operation == "account_error":
                gateway.account()
            else:
                gateway.request(engine="google", q="apps")
        assert original_key not in str(caught.value)


@pytest.mark.parametrize(
    "language,country,expected",
    [
        ("en", "in", "en-us"),
        ("en", "gb", "en-gb"),
        ("de", "us", "de-de"),
        ("hi", "us", "hi-in"),
        ("ja", "us", "ja-jp"),
        ("fr", "ca", "fr-ca"),
        ("pt", "in", "pt-pt"),
        ("en-au", "in", "en-au"),
    ],
)
def test_apple_language_uses_supported_locale_without_changing_country(language, country, expected):
    from apptrail.engines import apple_language

    assert apple_language(language, country) == expected
