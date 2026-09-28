export function matrixRows(data, source) {
  const observations = new Map();
  for (const row of [...data.observations].sort(
    (a, b) => a.checked_at - b.checked_at || a.run_id - b.run_id,
  )) {
    const key = `${row.monitor_id}:${row.app_id}`;
    const pair = observations.get(key) || [];
    pair.push(row);
    observations.set(key, pair);
  }
  return data.monitors
    .filter((m) => m.source === source)
    .sort(
      (a, b) =>
        a.country.localeCompare(b.country) ||
        a.language.localeCompare(b.language) ||
        a.id - b.id,
    )
    .map((monitor) => ({
      monitor,
      cells: data.apps.map((app) => {
        const rows = observations.get(`${monitor.id}:${app.id}`) || [];
        const latest = rows.at(-1),
          previous = rows.at(-2);
        return {
          app,
          tracked: monitor.app_ids.includes(app.id),
          latest,
          delta:
            latest?.status === "success" &&
            previous?.status === "success" &&
            latest.data.position &&
            previous.data.position
              ? previous.data.position - latest.data.position
              : null,
        };
      }),
    }));
}

export function textDiff(before = "", after = "") {
  const a = String(before ?? "").match(/\s+|[^\s]+/g) || [];
  const b = String(after ?? "").match(/\s+|[^\s]+/g) || [];
  if (a.length * b.length > 2000000) {
    let prefix = 0,
      suffix = 0;
    while (prefix < a.length && prefix < b.length && a[prefix] === b[prefix])
      prefix++;
    while (
      suffix < a.length - prefix &&
      suffix < b.length - prefix &&
      a[a.length - suffix - 1] === b[b.length - suffix - 1]
    )
      suffix++;
    const segments = (tokens) => [
      { text: tokens.slice(0, prefix).join(""), changed: false },
      {
        text: tokens.slice(prefix, tokens.length - suffix).join(""),
        changed: true,
      },
      { text: suffix ? tokens.slice(-suffix).join("") : "", changed: false },
    ];
    return { before: segments(a), after: segments(b) };
  }
  const dp = Array.from(
    { length: a.length + 1 },
    () => new Uint16Array(b.length + 1),
  );
  for (let i = a.length - 1; i >= 0; i--)
    for (let j = b.length - 1; j >= 0; j--)
      dp[i][j] =
        a[i] === b[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const result = { before: [], after: [] };
  let i = 0,
    j = 0;
  const add = (side, text, changed) => {
    const last = result[side].at(-1);
    if (last?.changed === changed) last.text += text;
    else result[side].push({ text, changed });
  };
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      add("before", a[i++], false);
      add("after", b[j++], false);
    } else if (j < b.length && (i === a.length || dp[i][j + 1] >= dp[i + 1][j]))
      add("after", b[j++], true);
    else add("before", a[i++], true);
  }
  return result;
}

export function sameMedia(before, after) {
  if (!before || !after) return before === after;
  if (before.asset_id && after.asset_id) {
    if (before.pixel_hash && after.pixel_hash)
      return before.pixel_hash === after.pixel_hash;
    return before.asset_id === after.asset_id;
  }
  return Boolean(before.url && before.url === after.url);
}

export function mediaChanges(before, after) {
  return after.map((item, index) => {
    const oldIndex = before.findIndex(
      (old) => sameMedia(old, item),
    );
    return {
      ...item,
      label:
        oldIndex < 0
          ? "Added"
          : oldIndex !== index
            ? `Moved from ${oldIndex + 1}`
            : "Unchanged",
    };
  });
}
