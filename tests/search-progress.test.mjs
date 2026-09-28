import { test } from "node:test";
import assert from "node:assert/strict";
import {
  searchProgress,
  trackingSummary,
} from "../src/apptrail/static/tracking.js";

test("initial queue progresses through retries without counting old history", () => {
  let progress = searchProgress(null, { success: 100, error: 4, queued: 17 });
  assert.equal(progress.total, 17);
  assert.equal(progress.completed, 0);
  progress = searchProgress(progress, {
    success: 105,
    error: 4,
    queued: 11,
    running: 1,
  });
  assert.equal(progress.completed, 5);
  assert.equal(progress.total, 17);
  progress = searchProgress(progress, { success: 105, error: 4, queued: 12 });
  assert.equal(progress.total, 17);
  assert.equal(progress.failed, 0);
  progress = searchProgress(progress, { success: 116, error: 5 });
  assert.equal(progress.pending, 0);
  assert.equal(progress.succeeded, 16);
  assert.equal(progress.failed, 1);
  assert.equal(progress.completed, 17);
});

test("new checks join a batch and cancellation never looks like success", () => {
  let progress = searchProgress(null, { queued: 2 });
  progress = searchProgress(progress, { success: 1, queued: 4 });
  assert.equal(progress.total, 5);
  progress = searchProgress(progress, { success: 1, cancelled: 4 });
  assert.equal(progress.cancelled, 4);
  assert.equal(progress.succeeded, 1);
  assert.equal(progress.pending, 0);
  progress = searchProgress(progress, { success: 1, cancelled: 4, queued: 1 });
  assert.equal(progress.total, 1);
  assert.equal(progress.cancelled, 0);
  assert.equal(progress.succeeded, 0);
});

test("an idle page does not announce historical jobs as a new completion", () => {
  assert.equal(
    searchProgress(null, { success: 40, error: 6, cancelled: 2 }),
    null,
  );
});

test("schedule counts active queries once, including shared apps and regional costs", () => {
  const apps = [{ id: 1 }, { id: 2 }, { id: 3, archived: true }];
  const base = {
    query: "tasks",
    country: "us",
    language: "en",
    frequency: "daily",
    depth: 1,
    enabled: true,
    app_ids: [1, 2],
  };
  const monitors = [
    { ...base, source: "apple_app_store" },
    { ...base, source: "google_play", depth: 3 },
    { ...base, source: "google_ai_overview" },
    { ...base, source: "bing_copilot", country: "global", language: "auto" },
    { ...base, source: "apple_app_store", country: "gb", frequency: "monthly" },
    { ...base, query: "paused", source: "apple_app_store", enabled: false },
    { ...base, query: "archived", source: "google_play", app_ids: [3] },
    { ...base, query: "detached", source: "google_play", app_ids: [] },
  ];
  const summary = trackingSummary(monitors, apps);
  assert.equal(summary.active.length, 5);
  assert.equal(summary.groups.length, 4);
  assert.deepEqual(summary.usage, { created: 5, min: 181, max: 211 });
  assert.deepEqual(
    summary.groups.find((g) => g.source === "google_play").usage,
    { created: 1, min: 90, max: 90 },
  );
  assert.deepEqual(
    summary.groups.find((g) => g.source === "apple_app_store").usage,
    { created: 2, min: 31, max: 31 },
  );
});

test("schedule shows zero usage when all queries are paused or untracked", () => {
  const result = trackingSummary(
    [{ enabled: true, app_ids: [1] }],
    [{ id: 1, archived: true }],
  );
  assert.deepEqual(result.active, []);
  assert.deepEqual(result.usage, { created: 0, min: 0, max: 0 });
});
