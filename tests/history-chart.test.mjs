import { test } from "node:test";
import assert from "node:assert/strict";
import { localDateValue, visibilitySeries } from "../src/apptrail/static/history-chart.js";

const row = (country, position, extra = {}) => ({
  checked_at: Date.parse("2026-09-23T12:00:00Z") / 1000,
  status: "success",
  params: {
    country,
    source: "apple_app_store",
    query: "tasks",
    ...extra.params,
  },
  data: { position, ...extra.data },
  ...Object.fromEntries(
    Object.entries(extra).filter(([key]) => !["params", "data"].includes(key)),
  ),
});

test("country comparison isolates the selected source, query and countries", () => {
  const result = visibilitySeries(
    [
      row("us", 2),
      row("us", 4),
      row("gb", 9),
      row("de", 1),
      row("us", 100, { params: { query: "different query" } }),
      row("gb", 1, { params: { source: "google_play" } }),
    ],
    {
      groupBy: "country",
      source: "apple_app_store",
      query: "tasks",
      countries: ["us", "gb"],
    },
  );
  assert.deepEqual(result, {
    dates: ["2026-09-23"],
    series: [
      { key: "us", data: [3] },
      { key: "gb", data: [9] },
    ],
  });
});

test("failed and missing rankings remain gaps instead of zeroes", () => {
  const nextDay = Date.parse("2026-09-24T12:00:00Z") / 1000;
  const result = visibilitySeries(
    [
      row("us", 3),
      row("gb", null),
      row("us", 1, { status: "error", checked_at: nextDay }),
      row("gb", 5, { checked_at: nextDay }),
    ],
    { groupBy: "country", countries: ["us", "gb", "in"] },
  );
  assert.deepEqual(result.series, [
    { key: "us", data: [3, null] },
    { key: "gb", data: [null, 5] },
    { key: "in", data: [null, null] },
  ]);
});

test("AI comparison counts returned answers and excludes global Copilot", () => {
  const answer = (country, mentioned, extra = {}) =>
    row(country, null, {
      params: { source: "google_ai_mode" },
      data: { mentioned, answer_available: true, ...extra },
    });
  const result = visibilitySeries(
    [
      answer("us", true),
      answer("us", false),
      answer("us", true, { answer_available: false }),
      answer("gb", false),
      row("global", null, {
        params: { source: "bing_copilot" },
        data: { mentioned: true, answer_available: true },
      }),
    ],
    { kind: "ai", groupBy: "country", countries: ["us", "gb"] },
  );
  assert.deepEqual(result.series, [
    { key: "us", data: [50] },
    { key: "gb", data: [0] },
  ]);
});

test("source view still averages countries per source", () => {
  const result = visibilitySeries([
    row("us", 2),
    row("gb", 8),
    row("us", 4, { params: { source: "google_play" } }),
  ]);
  assert.deepEqual(result.series, [
    { key: "apple_app_store", data: [5] },
    { key: "google_play", data: [4] },
  ]);
});

test("an empty country selection never falls back to all countries", () => {
  assert.deepEqual(
    visibilitySeries([row("us", 2)], { groupBy: "country", countries: [] }),
    { dates: [], series: [] },
  );
});

test("days with no checks remain calendar gaps across a year boundary", () => {
  const result = visibilitySeries([
    row("us", 2, { checked_at: Date.parse("2026-12-30T12:00:00Z") / 1000 }),
    row("us", 6, { checked_at: Date.parse("2027-01-02T12:00:00Z") / 1000 }),
    row("us", 8, {
      checked_at: Date.parse("2027-01-02T12:00:00Z") / 1000,
      params: { source: "google_play" },
    }),
  ]);
  assert.deepEqual(result, {
    dates: ["2026-12-30", "2026-12-31", "2027-01-01", "2027-01-02"],
    series: [
      { key: "apple_app_store", data: [2, null, null, 6] },
      { key: "google_play", data: [null, null, null, 8] },
    ],
  });
});

test("custom date inputs keep the local calendar day around midnight and new year", () => {
  assert.equal(localDateValue(new Date(2026, 0, 1, 0, 15)), "2026-01-01");
  assert.equal(localDateValue(new Date(2026, 11, 31, 23, 45)), "2026-12-31");
});
