from types import SimpleNamespace

import pytest

from apptrail.engines import answer_content, sanitize, store_identity, store_results
from apptrail.matching import match_app


def app(name="Todo Example", website=""):
    return SimpleNamespace(name=name, aliases=[], website=website)


def listing():
    return SimpleNamespace(platform="ios", external_id="123", title="Todo Example")


def test_identifier_beats_identical_title():
    result = {
        "kind": "store",
        "items": [
            {
                "platform": "ios",
                "external_id": "999",
                "title": "Todo Example",
                "primary": True,
                "position": 1,
            }
        ],
        "results_checked": 1,
    }
    assert not match_app(app(), [listing()], result)["found"]
    result["items"][0].update(
        external_id="123", url="https://apps.apple.com/us/app/id123", section="Search results"
    )
    assert match_app(app(), [listing()], result)["position"] == 1


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://apps.apple.com/us/app/todo/id123?platform=iphone", ("ios", "123")),
        ("https://apps.apple.com/us/app/123", ("ios", "123")),
        ("https://apps.apple.com/us/app/2048/id840919914", ("ios", "840919914")),
        (
            "https://play.google.com/store/apps/details?id=com.todoist&hl=en",
            ("android", "com.todoist"),
        ),
        ("https://apps.apple.com.attacker.test/us/app/id123", None),
        ("https://play.google.com/store/apps/developer?id=com.todoist", None),
    ],
)
def test_store_url_identity(url, expected):
    assert store_identity(url) == expected


def test_mentions_do_not_match_substrings_or_reference_titles():
    result = {
        "kind": "ai",
        "text": "ExampleOther is useful.",
        "references": [{"title": "Todo Example", "link": "https://example.org/review"}],
        "answer_available": True,
    }
    assert not match_app(app(), [listing()], result)["mentioned"]
    result["text"] = "Try TODO EXAMPLE today."
    assert match_app(app(), [listing()], result)["mentioned"]


def test_ambiguous_names_and_exact_citations():
    result = {
        "kind": "ai",
        "text": "Use Notes for notes.",
        "references": [],
        "answer_available": True,
    }
    ambiguous = match_app(app("Notes"), [], result)
    assert ambiguous["possible"] and not ambiguous["found"]
    result["references"] = [{"title": "App", "link": "https://apps.apple.com/us/app/id123"}]
    assert match_app(app("Notes"), [listing()], result)["app_link"]


def test_website_citations_respect_path_and_domain():
    result = {
        "kind": "ai",
        "text": "Here are tools.",
        "references": [{"link": "https://example.com/app-two", "title": "Another app"}],
        "answer_available": True,
    }
    assert not match_app(app(website="https://example.com/app-one"), [], result)["cited"]
    result["references"][0]["link"] = "https://example.com/app-one/features"
    matched = match_app(app(website="https://example.com/app-one"), [], result)
    assert matched["website_cited"] and not matched["app_link"]


def test_nested_ai_text_and_tables_exclude_reference_titles():
    text, refs = answer_content(
        {
            "header": "Apps",
            "text_blocks": [
                {"list": [{"snippet": "Todo Example", "list": [{"snippet": "Offline support"}]}]},
                {"table": [["Name", "Price"], ["Example", "Free"]]},
            ],
            "references": [{"title": "Not answer text", "link": "https://example.com"}],
        }
    )
    assert "Offline support" in text and "Example | Free" in text
    assert "Not answer text" not in text and len(refs) == 1


def test_secrets_redacted_recursively():
    result = sanitize(
        {
            "api_key": "private",
            "nested": [
                {"url": "https://serpapi.com/search?api_key=private&q=test", "message": "private"}
            ],
        },
        "private",
    )
    assert result == {
        "nested": [
            {
                "url": "https://serpapi.com/search?api_key=[REDACTED]&q=test",
                "message": "[REDACTED]",
            }
        ]
    }


def test_google_play_sections_and_highlights():
    data = {
        "organic_results": [
            {"title": "Apps", "items": [{"product_id": "a.b", "title": "One"}]},
            {"title": "Similar apps", "items": [{"product_id": "c.d", "title": "Two"}]},
        ],
        "app_highlight": {"product_id": "e.f", "title": "Feature"},
    }
    rows = store_results(data, "android")
    assert rows[0]["primary"] and not rows[1]["primary"]
    assert rows[1]["position"] == 1
    assert rows[2]["position"] is None
