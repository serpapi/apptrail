import test from "node:test";
import assert from "node:assert/strict";
import {
  competitorQueryGroups,
  selectedCompetitorMonitors,
} from "../src/apptrail/static/competitors.js";

test("selects saved source/query/country combinations without inventing missing markets", () => {
  const monitors = [
    { id: 1, source: "apple_app_store", query: "planner", country: "us" },
    { id: 2, source: "apple_app_store", query: "planner", country: "gb" },
    { id: 3, source: "google_play", query: "planner", country: "us" },
    {
      id: 4,
      source: "google_ai_mode",
      query: "Suggest planners",
      country: "gb",
    },
    {
      id: 5,
      source: "bing_copilot",
      query: "Suggest planners",
      country: "global",
    },
  ];
  const groups = competitorQueryGroups(monitors);
  const keys = new Set(groups.map((group) => group.key));
  assert.equal(groups.length, 4);
  assert.deepEqual(
    selectedCompetitorMonitors(monitors, keys, new Set(["gb"])).map(
      (monitor) => monitor.id,
    ),
    [2, 4],
  );
  assert.deepEqual(
    selectedCompetitorMonitors(monitors, keys, new Set(["global"])).map(
      (monitor) => monitor.id,
    ),
    [5],
  );
  keys.delete(groups.find((group) => group.source === "apple_app_store").key);
  assert.deepEqual(
    selectedCompetitorMonitors(
      monitors,
      keys,
      new Set(["us", "gb", "global"]),
    ).map((monitor) => monitor.id),
    [3, 4, 5],
  );
  assert.deepEqual(
    selectedCompetitorMonitors(monitors, new Set(), new Set(["us"])),
    [],
  );
});

test("groups source and query separately while retaining language and device variants", () => {
  const monitors = [
    {
      id: 1,
      source: "google_ai_mode",
      query: "a: b",
      country: "us",
      language: "en",
      device: "desktop",
    },
    {
      id: 2,
      source: "google_ai_mode",
      query: "a: b",
      country: "us",
      language: "es",
      device: "mobile",
      enabled: false,
    },
    { id: 3, source: "google_ai_mode", query: "a", country: "us" },
  ];
  const group = competitorQueryGroups(monitors).find(
    (item) => item.query === "a: b",
  );
  assert.deepEqual(
    selectedCompetitorMonitors(
      monitors,
      new Set([group.key]),
      new Set(["us"]),
    ).map((monitor) => monitor.id),
    [1, 2],
  );
});
