import json
from pathlib import Path

# SerpApi catalogs, filtered by live checks on 2026-09-24.
REGIONS = json.loads(Path(__file__).with_name("regions.json").read_text())


def supported_country(source, country):
    if source == "bing_copilot":
        return True
    return country in source_countries(source)


def source_countries(source):
    if source == "bing_copilot":
        return ["global"]
    group = "apple" if source == "apple_app_store" else "google"
    return REGIONS.get(source, REGIONS[group])
