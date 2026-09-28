export function monitorKey(monitor) {
  const global = monitor.source === "bing_copilot";
  const store = ["apple_app_store", "google_play"].includes(monitor.source);
  return JSON.stringify([
    monitor.query.trim(),
    monitor.source,
    global ? "global" : monitor.country,
    global ? "auto" : monitor.language,
    global || monitor.source === "google_play" ? "" : monitor.device || "",
    store ? Number(monitor.depth || 1) : 1,
  ]);
}

export function expandQueries(options) {
  const countries = [...new Set(options.countries)];
  const entries = new Map();
  for (const source of options.sources) {
    const store = ["apple_app_store", "google_play"].includes(source);
    const queries = (store ? options.storeQueries : options.aiQueries)
      .map((query) => query.trim())
      .filter(Boolean);
    if (queries.length && source !== "bing_copilot" && !countries.length)
      throw new Error("Select at least one country.");
    for (const query of queries) {
      for (const country of source === "bing_copilot" ? ["us"] : countries) {
        const monitor = {
          query,
          source,
          country,
          language: options.language,
          frequency: options.frequency,
          device: options.device || "",
          depth: store ? Number(options.depth || 1) : 1,
          app_ids: options.appIds,
        };
        entries.set(monitorKey(monitor), monitor);
      }
    }
  }
  if (entries.size > 2000)
    throw new Error(
      "Add up to 2,000 country and query combinations at a time.",
    );
  return [...entries.values()];
}

export function searchUsage(monitors, existing = []) {
  const known = new Set(existing.map(monitorKey));
  let min = 0,
    max = 0,
    created = 0;
  for (const monitor of monitors) {
    const key = monitorKey(monitor);
    if (known.has(key)) continue;
    known.add(key);
    created++;
    const checks =
      30 /
      { daily: 1, weekly: 7, biweekly: 14, monthly: 30 }[monitor.frequency];
    const calls = monitor.source === "google_play" ? monitor.depth : 1;
    min += checks * calls;
    max += checks * (calls + (monitor.source === "google_ai_overview" ? 1 : 0));
  }
  return { min: Math.round(min), max: Math.round(max), created };
}

export function trackingSummary(monitors, apps) {
  const activeApps = new Set(
    apps.filter((app) => !app.archived).map((app) => app.id),
  );
  const active = monitors.filter(
    (monitor) =>
      monitor.enabled && monitor.app_ids.some((id) => activeApps.has(id)),
  );
  const groups = [...new Set(active.map((monitor) => monitor.source))].map(
    (source) => {
      const queries = active.filter((monitor) => monitor.source === source);
      return { source, queries, usage: searchUsage(queries) };
    },
  );
  return { active, groups, usage: searchUsage(active) };
}

export function queryGroups(
  monitors,
  apps,
  { kind, appId = 0, source = "", country = "" } = {},
) {
  const activeApps = new Set(
    apps.filter((app) => !app.archived).map((app) => app.id),
  );
  const groups = new Map();
  for (const monitor of monitors) {
    const category = ["apple_app_store", "google_play"].includes(monitor.source)
      ? "store"
      : "ai";
    if (
      (kind && kind !== category) ||
      (appId && !monitor.app_ids.includes(appId)) ||
      !monitor.app_ids.some((id) => activeApps.has(id))
    )
      continue;
    const key = JSON.stringify([category, monitor.query.trim()]);
    if (!groups.has(key))
      groups.set(key, {
        key,
        query: monitor.query.trim(),
        monitors: [],
        visible: [],
      });
    const group = groups.get(key);
    group.monitors.push(monitor);
    if (
      (!source || monitor.source === source) &&
      (!country || monitor.country === country)
    )
      group.visible.push(monitor);
  }
  return [...groups.values()]
    .filter((group) => group.visible.length)
    .map((group) => ({
      ...group,
      id: Math.min(...group.monitors.map((monitor) => monitor.id)),
    }));
}

export function searchProgress(previous, jobs = {}) {
  const running = jobs.running || 0,
    queued = jobs.queued || 0;
  const pending = running + queued;
  if (!pending && !previous?.pending) return previous || null;
  const terminal = {
    success: jobs.success || 0,
    error: jobs.error || 0,
    cancelled: jobs.cancelled || 0,
  };
  const baseline = previous?.pending ? previous.baseline : terminal;
  const succeeded = Math.max(0, terminal.success - baseline.success);
  const failed = Math.max(0, terminal.error - baseline.error);
  const cancelled = Math.max(0, terminal.cancelled - baseline.cancelled);
  const completed = succeeded + failed + cancelled;
  return {
    baseline,
    running,
    queued,
    pending,
    succeeded,
    failed,
    cancelled,
    completed,
    total: pending + completed,
  };
}
