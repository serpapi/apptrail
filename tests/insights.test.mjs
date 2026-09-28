import { test } from "node:test";
import assert from "node:assert/strict";
import {
  textDiff,
  matrixRows,
  mediaChanges,
  sameMedia,
} from "../src/apptrail/static/insights-data.js";

test("text comparisons retain whitespace and isolate additions and removals", () => {
  const before = "Plan your tasks.\nWork alone.";
  const after = "Plan your tasks.\nWork together.";
  const diff = textDiff(before, after);
  assert.equal(diff.before.map((x) => x.text).join(""), before);
  assert.equal(diff.after.map((x) => x.text).join(""), after);
  assert.equal(
    diff.after
      .filter((x) => x.changed)
      .map((x) => x.text)
      .join(""),
    "together.",
  );
  assert.equal(
    diff.before
      .filter((x) => x.changed)
      .map((x) => x.text)
      .join(""),
    "alone.",
  );
});

test("large listing diffs retain shared text and mark the changed ending", () => {
  const before = "a ".repeat(4000) + "old ending";
  const after = "a ".repeat(4000) + "new ending";
  const result = textDiff(before, after);
  assert.equal(result.before.map((x) => x.text).join(""), before);
  assert.equal(result.after.map((x) => x.text).join(""), after);
  assert.equal(result.before.filter((x) => x.changed).map((x) => x.text).join(""), "old");
  assert.equal(result.after.filter((x) => x.changed).map((x) => x.text).join(""), "new");
});

test("matrix preserves independent locale/device rows and latest failures", () => {
  const monitors = [1, 2].map((id) => ({
    id,
    query: "tasks",
    country: "us",
    language: id === 1 ? "en" : "es",
    source: "apple_app_store",
    app_ids: [1],
  }));
  const data = {
    monitors,
    apps: [{ id: 1 }, { id: 2 }],
    observations: [
      {
        monitor_id: 1,
        app_id: 1,
        checked_at: 10,
        run_id: 1,
        status: "success",
        data: { position: 3 },
      },
      {
        monitor_id: 1,
        app_id: 1,
        checked_at: 20,
        run_id: 2,
        status: "error",
        data: {},
      },
    ],
  };
  const rows = matrixRows(data, "apple_app_store");
  assert.equal(rows.length, 2);
  assert.equal(rows[0].cells[0].latest.status, "error");
  assert.equal(rows[0].cells[0].delta, null);
  assert.equal(rows[0].cells[1].tracked, false);
  assert.equal(rows[1].cells[0].latest, undefined);
});

test("screenshot changes distinguish reordered, new and unchanged images", () => {
  const before = [{ asset_id: "one" }, { asset_id: "two" }, { asset_id: "same" }];
  const after = [
    { asset_id: "two" },
    { asset_id: "one" },
    { asset_id: "same" },
    { asset_id: "three" },
  ];
  assert.deepEqual(
    mediaChanges(before, after).map((x) => x.label),
    ["Moved from 2", "Moved from 1", "Unchanged", "Added"],
  );
});

test("metadata-only changes keep screenshots unchanged and preserve reorder labels", () => {
  const before = [
    { asset_id: "old-a", pixel_hash: "pixels-a", url: "same-url" },
    { asset_id: "old-b", pixel_hash: "pixels-b" },
  ];
  const after = [
    { asset_id: "new-a", pixel_hash: "pixels-a", url: "new-url" },
    { asset_id: "new-b", pixel_hash: "pixels-b" },
  ];
  assert.deepEqual(mediaChanges(before, after).map((x) => x.label), ["Unchanged", "Unchanged"]);
  assert.deepEqual(mediaChanges(before, after.toReversed()).map((x) => x.label), ["Moved from 2", "Moved from 1"]);
  assert.ok(sameMedia(before[0], after[0]));
  assert.equal(sameMedia(before[0], { asset_id: "changed", pixel_hash: "new-pixels", url: "same-url" }), false);
  assert.ok(sameMedia(before[0], { asset_id: null, pixel_hash: null, url: "same-url" }));
  assert.ok(sameMedia(before[0], { asset_id: "old-a", url: "same-url" }));
});
