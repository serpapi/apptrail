import { lucideIcons } from "./vendor/lucide-icons.js";
import {
  expandQueries,
  monitorKey,
  searchUsage,
  trackingSummary,
  searchProgress,
  queryGroups,
} from "./tracking.js";
import { localDateValue, visibilitySeries } from "./history-chart.js";
import { createInsightsUI } from "./insights-ui.js";
import { createCompetitorUI } from "./competitors.js";
import { beginLoading, isLoading, placeLoadingIndicator } from "./loading.js";

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const sidebarPaths = {
  overview:
    '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  apps: '<rect x="5" y="2" width="14" height="20" rx="3"/><path d="M10 18h4M9 6h6"/>',
  chart: '<path d="M4 3v17h17M8 15l4-5 4 2 5-7"/>',
  sparkles:
    '<path d="m12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6L12 3ZM20 2v4M18 4h4"/>',
  activity: '<path d="M2 12h5l3-8 4 16 3-8h5"/>',
  history: '<path d="M3 12a9 9 0 1 0 2.6-6.4L3 8M3 3v5h5M12 7v5l3 2"/>',
};
const sidebarIcon = (name) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${sidebarPaths[name]}</svg>`;
const icon = (name) => {
  const item = lucideIcons[name] || lucideIcons.globe;
  return `<svg class="lucide lucide-${item.name}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${item.body}</svg>`;
};
const labels = {
  overview: "Overview",
  apps: "Apps and competitors",
  rankings: "Store rankings",
  ai: "AI visibility",
  activity: "Activity",
  "listing-history": "Listing history",
  settings: "Settings",
};
const sourceLabels = {
  apple_app_store: "App Store",
  google_play: "Google Play",
  google_ai_mode: "Google AI Mode",
  google_ai_overview: "AI Overview",
  bing_copilot: "Bing Copilot",
};
const sourceIcons = {
  apple_app_store: "smartphone",
  google_play: "play",
  google_ai_mode: "bot",
  google_ai_overview: "sparkles",
  bing_copilot: "message",
};
const sourceColors = {
  apple_app_store: "#377fea",
  google_play: "#278361",
  google_ai_mode: "#6937ea",
  google_ai_overview: "#b97813",
  bing_copilot: "#178098",
};
let csrfToken = "",
  accountName = "",
  leaving = false;
let state,
  dashboard,
  charts = [],
  appId = Number(localStorage.getItem("apptrail-app") || 0),
  days = 30,
  chartMode = "rank",
  chartView = "trend",
  chartGroup = "source",
  comparisonSource = "",
  comparisonQuery = "",
  comparisonCountries = null,
  customStart = "",
  customEnd = "",
  sourceFilter = "",
  countryFilter = "",
  regions,
  syncVersion = 0,
  renderRequested = false,
  comparisonRequest = 0;
let wizard = null,
  candidates = [],
  selectedCandidates = {};
let trackingState,
  progress = null,
  progressDismissed = false,
  trackingUnavailable = false;
const modal = $("#modal");
const insights = createInsightsUI({
  api,
  esc,
  icon,
  button: (...args) => button(...args),
  when: (...args) => when(...args),
  countryName: (...args) => countryName(...args),
  showModal,
  modal,
  sourceLabels,
  showRun,
  sync,
  render,
  heading,
  toast,
  state: () => state,
  appId: () => appId,
  countryFilter: () => countryFilter,
  setCountry: (value) => {
    countryFilter = value;
  },
  setApp: (value) => {
    appId = value;
    localStorage.setItem("apptrail-app", value);
  },
  appById: (id) => appById(id),
  regions: () => regions,
});
const competitors = createCompetitorUI({
  api,
  esc,
  icon,
  modal,
  sourceLabels,
  heading,
  render,
  sync,
  toast,
  button: (...args) => button(...args),
  state: () => state,
  appId: () => appId,
  countryName: (...args) => countryName(...args),
  sourceCountries: (...args) => sourceCountries(...args),
  countries: (...args) => countries(...args),
  languages: (...args) => languages(...args),
});
const route = () =>
  labels[location.hash.slice(1)] ? location.hash.slice(1) : "overview";
const selectedApp = () => state.apps.find((a) => a.id === appId);
const appById = (id) => state.apps.find((a) => a.id === id);
const num = (n) => Number(n).toLocaleString();
const compactNumber = new Intl.NumberFormat("en", {
  notation: "compact",
  maximumFractionDigits: 1,
});
function reviewCount(value, compact = true) {
  const text = String(value ?? "—").trim(),
    match = text.match(
      /^(\d+(?:,\d{3})*(?:\.\d+)?)\s*(ratings?\/reviews?|ratings?|reviews?)?$/i,
    );
  if (!match)
    return /ratings?|reviews?/i.test(text) ? text : `${text} ratings/reviews`;
  const count = Number(match[1].replaceAll(",", "")),
    label = match[2] || "ratings/reviews";
  return `${compact && count >= 1000 ? compactNumber.format(count) : num(count)} ${label}`;
}
const when = (timestamp) =>
  timestamp
    ? new Date(timestamp * 1000).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      })
    : "Not checked yet";
const ago = (timestamp) => {
  if (!timestamp) return "Not yet";
  const minutes = Math.max(0, Math.floor((Date.now() / 1000 - timestamp) / 60));
  return minutes < 1
    ? "Just now"
    : minutes < 60
      ? `${minutes}m ago`
      : minutes < 1440
        ? `${Math.floor(minutes / 60)}h ago`
        : `${Math.floor(minutes / 1440)}d ago`;
};
const platformSource = (platform) =>
  platform === "ios" ? "apple_app_store" : "google_play";
const sourceBadge = (source) =>
  `<span class="source-symbol ${esc(source)}" aria-hidden="true">${icon(sourceIcons[source] || "globe")}</span>`;
const appImage = (app, cls = "") =>
  app?.listings?.[0]?.icon
    ? `<img class="${cls}" src="${esc(app.listings[0].icon)}" alt="" loading="lazy">`
    : `<span class="app-placeholder">${esc((app?.name || "A").slice(0, 1))}</span>`;
const button = (text, action, style = "", extra = "") =>
  `<button class="button ${style}" data-action="${action}" ${extra}>${text}</button>`;
const statusTag = (status) =>
  `<span class="tag ${status === "success" ? "good" : status === "error" ? "error" : ["queued", "running"].includes(status) ? "warn" : "neutral"}">${esc(status || "Not checked")}</span>`;
const empty = (title, copy, action = "") =>
  `<div class="empty">${icon("chart")}<h3>${title}</h3><p>${copy}</p>${action}</div>`;
function toast(message, error = false) {
  if (leaving) return;
  const el = document.createElement("div");
  el.className = `toast ${error ? "error" : ""}`;
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), error ? 10000 : 4500);
}
async function api(path, options = {}) {
  const response = await fetch(`/api/${path}`, {
    ...options,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-AppTrail-Request": "1",
      "X-CSRF-Token": csrfToken,
      ...options.headers,
    },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  if (!response.ok) {
    if (response.status === 401 && path !== "auth/password") {
      leaving = true;
      document.body.replaceChildren();
      location.replace("/login");
      throw new Error("Sign in to AppTrail.");
    }
    let data;
    try {
      data = await response.json();
    } catch {
      data = {
        detail:
          response.status === 401
            ? "Sign in to your workspace."
            : "Something went wrong. Please try again.",
      };
    }
    throw new Error(data.detail || `Request failed (${response.status})`);
  }
  return response.json();
}
function observationRows(kind) {
  return (dashboard?.observations || []).filter(
    (row) =>
      (!sourceFilter || row.params.source === sourceFilter) &&
      (!countryFilter || row.params.country === countryFilter) &&
      (!kind ||
        (kind === "store") ===
          ["apple_app_store", "google_play"].includes(row.params.source)),
  );
}
function monitorRows(kind) {
  return state.monitors.filter(
    (m) =>
      (!appId || m.app_ids.includes(appId)) &&
      (!sourceFilter || m.source === sourceFilter) &&
      (!countryFilter || m.country === countryFilter) &&
      (!kind ||
        (kind === "store") ===
          ["apple_app_store", "google_play"].includes(m.source)),
  );
}
function metric(rows, field) {
  const valid = rows.filter(
    (r) => r.status === "success" && r.data.answer_available !== false,
  );
  return {
    rate: valid.length
      ? Math.round(
          (valid.filter((r) => r.data[field]).length / valid.length) * 100,
        )
      : null,
    total: valid.length,
    hits: valid.filter((r) => r.data[field]).length,
  };
}
function percent(value) {
  return value === null ? "—" : `${value}<small>%</small>`;
}
function setupChecklist(compact = false) {
  const progress = state.onboarding;
  if (!progress?.visible) return "";
  const steps = [
    {
      id: "app",
      title: "Connect your app",
      done: "App connected",
      copy: "Add an App Store or Google Play listing.",
      action: "add-app",
    },
    {
      id: "store",
      title: "Add store keywords",
      done: "Store keywords added",
      copy: "Choose the store searches you want to follow.",
      action: "add-store",
    },
    {
      id: "ai",
      title: "Add AI questions",
      done: "AI questions added",
      copy: "Track questions that could mention your app.",
      action: "add-ai",
    },
  ];
  return `<section class="panel setup-checklist ${compact ? "compact" : ""}" aria-labelledby="setup-checklist-title"><div class="checklist-heading"><div><h2 id="setup-checklist-title">Finish setting up</h2><p>${progress.completed.length} of 3 complete</p></div>${button(`Dismiss ${icon("close")}`, "dismiss-checklist", "checklist-dismiss")}</div><ol class="checklist-items">${steps
    .map((step, i) => {
      const done = progress.completed.includes(step.id),
        skipped = progress.skipped.includes(step.id);
      return `<li class="${done ? "complete" : skipped ? "skipped" : ""}"><span class="welcome-step-number" aria-hidden="true">${done ? icon("check") : i + 1}</span><div class="checklist-item-content">${done ? `<h3>${step.done}</h3>` : `<button class="checklist-action" data-action="${step.action}">${step.title}${icon("arrow")}</button><p>${step.copy}</p>`}${!done ? (skipped ? '<span class="checklist-skipped">Skipped</span>' : `<button class="text-link checklist-skip" data-action="skip-checklist-step" data-step="${step.id}" aria-label="Skip: ${step.title}">Skip for now</button>`) : ""}</div></li>`;
    })
    .join("")}</ol></section>`;
}

function chartOptions() {
  const monitors = state.monitors.filter(
    (m) =>
      (!appId || m.app_ids.includes(appId)) &&
      (!sourceFilter || m.source === sourceFilter) &&
      (chartMode === "rank") ===
        ["apple_app_store", "google_play"].includes(m.source) &&
      m.country !== "global",
  );
  const sources = Object.keys(sourceLabels).filter((source) =>
    monitors.some((m) => m.source === source),
  );
  if (!sources.includes(comparisonSource)) {
    comparisonSource = sources[0] || "";
    comparisonQuery = "";
    comparisonCountries = null;
  }
  if (!sources.length) chartGroup = "source";
  const queries = [
    ...new Set(
      monitors.filter((m) => m.source === comparisonSource).map((m) => m.query),
    ),
  ].sort();
  if (!queries.includes(comparisonQuery)) comparisonQuery = "";
  const countries = [
    ...new Set(
      monitors
        .filter(
          (m) =>
            m.source === comparisonSource &&
            (!comparisonQuery || m.query === comparisonQuery),
        )
        .map((m) => m.country),
    ),
  ].sort((a, b) => countryName(a).localeCompare(countryName(b)));
  return {
    sources,
    queries,
    countries,
    selected:
      comparisonCountries === null
        ? countries
        : comparisonCountries.filter((c) => countries.includes(c)),
  };
}

function historyPanel() {
  const viewControl = `<div class="history-view-toggle chart-switcher" role="group" aria-label="Chart view">${["trend", "distribution"].map((view) => `<button data-action="history-view" data-view="${view}" aria-pressed="${chartView === view}" class="${chartView === view ? "active" : ""}">${view === "trend" ? "Trend" : "Distribution"}</button>`).join("")}</div>`;
  if (chartView === "distribution")
    return `<div class="panel history-panel"><div class="panel-head"><div><h2>Where your keywords rank</h2><p class="panel-subtitle">Positions across your tracked store searches.</p></div>${viewControl}</div>${insights.distributionPanel()}</div>`;
  const options = chartOptions(),
    comparing = chartGroup === "country";
  return `<div class="panel history-panel"><div class="panel-head"><div><h2>${comparing ? (chartMode === "rank" ? "Store rankings by country" : "AI mentions by country") : chartMode === "rank" ? "Average store position over time" : "AI mention rate over time"}</h2><p class="panel-subtitle">${chartMode === "rank" ? "Daily mean of ranked checks (UTC). Checked queries can vary by day; missing ranks are excluded. Lower is better." : "Daily mention rate among returned AI answers (UTC). Checked questions can vary by day."}</p></div>${viewControl}<div class="chart-toggle chart-switcher" role="group" aria-label="Chart source">${["rank", "ai"].map((mode) => `<button data-action="chart-mode" data-mode="${mode}" class="${chartMode === mode ? "active" : ""}" aria-pressed="${chartMode === mode}">${mode === "rank" ? "Stores" : "AI"}</button>`).join("")}</div></div>
    ${options.sources.length ? `<div class="chart-controls"><label><span>Compare</span><select class="input" id="chart-group"><option value="source" ${comparing ? "" : "selected"}>Sources</option><option value="country" ${comparing ? "selected" : ""}>Countries</option></select></label>${comparing ? `<label><span>Source</span><select class="input" id="comparison-source">${options.sources.map((source) => `<option value="${source}" ${comparisonSource === source ? "selected" : ""}>${sourceLabels[source]}</option>`).join("")}</select></label><label class="chart-query"><span>${chartMode === "rank" ? "Keyword" : "Question"}</span><select class="input" id="comparison-query"><option value="">${chartMode === "rank" ? "All keywords" : "All questions"}</option>${options.queries.map((query) => `<option value="${esc(query)}" ${comparisonQuery === query ? "selected" : ""}>${esc(query)}</option>`).join("")}</select></label><details class="chart-country-picker"><summary>Countries <span>${options.selected.length} selected</span></summary><div class="chart-country-actions">${button("Select all", "chart-countries-all", "small ghost")}${button("Clear", "chart-countries-clear", "small ghost")}</div><div class="chart-country-options">${options.countries.map((country) => `<label><input type="checkbox" data-chart-country="${country}" ${options.selected.includes(country) ? "checked" : ""}>${esc(countryName(country))}</label>`).join("")}</div></details>` : ""}</div>` : ""}
    <div class="chart-area"><canvas id="trend-chart" aria-label="${comparing ? "Country comparison" : "Visibility history"} chart · ${esc(comparing ? sourceLabels[comparisonSource] : countryFilter ? countryName(countryFilter) : "All countries")}" role="img"></canvas></div><div class="legend" id="trend-legend"></div></div>`;
}
function latestPairs(kind) {
  const pairs = new Map();
  for (const row of observationRows(kind)) {
    const key = `${row.monitor_id}:${row.app_id}`;
    if (!pairs.has(key)) pairs.set(key, []);
    pairs.get(key).push(row);
  }
  return [...pairs.values()].map((rows) => ({
    latest: rows.at(-1),
    previous: rows.filter((r) => r.status === "success").slice(-2, -1)[0],
  }));
}
function heading(
  title,
  subtitle,
  actions = "",
  eyebrow = "WORKSPACE OVERVIEW",
) {
  return `<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p>${subtitle}</p></div><div class="actions">${actions}</div></div>`;
}
const countryName = (code) =>
  code === "global"
    ? "Global (Bing Copilot)"
    : regions.countries[code] || code.toUpperCase();
function sourceInView(source) {
  const store = ["apple_app_store", "google_play"].includes(source);
  return route() === "rankings" ? store : route() === "ai" ? !store : true;
}
function availableCountries(data = state) {
  const codes = data.monitors
    .filter(
      (m) =>
        (!appId || m.app_ids.includes(appId)) &&
        sourceInView(m.source) &&
        (!sourceFilter || m.source === sourceFilter),
    )
    .map((m) => m.country);
  if (route() === "activity" && !sourceFilter) {
    for (const app of data.apps.filter((a) => !appId || a.id === appId))
      codes.push(...app.listings.map((l) => l.country));
  }
  return [...new Set(codes)].sort((a, b) =>
    countryName(a).localeCompare(countryName(b)),
  );
}
function scopeParams() {
  const params = new URLSearchParams();
  if (appId) params.set("app_id", appId);
  if (sourceFilter) params.set("source", sourceFilter);
  if (countryFilter) params.set("country", countryFilter);
  return params;
}
function filters() {
  return `<div class="filters"><div class="filter-left"><label class="visually-hidden" for="app-filter">Filter by app</label><select id="app-filter"><option value="0">All active apps</option>${state.apps
    .filter((a) => !a.archived)
    .map(
      (a) =>
        `<option value="${a.id}" ${a.id === appId ? "selected" : ""}>${esc(a.name)}</option>`,
    )
    .join(
      "",
    )}</select><label class="visually-hidden" for="source-filter">Filter by source</label><select id="source-filter"><option value="">All sources</option>${Object.entries(
    sourceLabels,
  )
    .filter(([source]) => sourceInView(source))
    .map(
      ([s, l]) =>
        `<option value="${s}" ${sourceFilter === s ? "selected" : ""}>${l}</option>`,
    )
    .join("")}</select>${
    availableCountries().length
      ? `<label class="visually-hidden" for="country-filter">Filter by country</label><select id="country-filter"><option value="">All countries</option>${availableCountries()
          .map(
            (code) =>
              `<option value="${code}" ${countryFilter === code ? "selected" : ""}>${esc(countryName(code))}</option>`,
          )
          .join("")}</select>`
      : ""
  }</div><div class="actions">${days === 0 ? `<div class="custom-dates"><input class="input" id="date-start" type="date" aria-label="Start date" value="${customStart}"><span>–</span><input class="input" id="date-end" type="date" aria-label="End date" value="${customEnd}"></div>` : ""}<div class="range" aria-label="Date range">${[7, 30, 90, 0].map((d) => `<button data-action="range" data-days="${d}" class="${days === d ? "active" : ""}" aria-pressed="${days === d}">${d ? d + " days" : "Custom"}</button>`).join("")}</div></div></div>`;
}
function stat(title, value, foot, symbol) {
  return `<div class="stat-card"><div class="stat-title">${title}${icon(symbol)}</div><div class="stat-value">${value}</div><div class="stat-foot">${foot}</div></div>`;
}
function recentActivity() {
  return `<div class="panel"><div class="panel-head"><h2>Recent activity</h2><a class="text-link" href="#activity">View all ${icon("arrow")}</a></div><div class="activity-list">${
    dashboard.runs
      .slice(0, 3)
      .map(
        (r) =>
          `<div class="activity-item"><span class="status-dot ${r.status === "error" ? "error" : ""}"></span><button class="activity-text cell-button" data-action="run" data-id="${r.id}">${esc(r.kind === "listing_history" ? "Listing history" : sourceLabels[r.params.source] || "Listing refresh")} ${r.status === "success" ? "checked" : esc(r.status)}<span>${esc(r.params.query || r.params.external_id)} · ${esc(countryName(r.params.country || "global"))}</span></button><span class="activity-time">${ago(r.started_at || r.created_at)}</span></div>`,
      )
      .join("") || '<p class="muted">No checks in this date range.</p>'
  }</div></div>`;
}
const expandedQueries = new Set();
const initializedQuerySections = new Set();
function pageQueryGroups(kind = route() === "ai" ? "ai" : "store") {
  return queryGroups(state.monitors, state.apps, {
    kind,
    appId,
    source: sourceFilter,
    country: countryFilter,
  });
}
function groupSchedule(group) {
  const frequencies = [
    ...new Set(group.monitors.map((monitor) => monitor.frequency)),
  ];
  return frequencies.length === 1
    ? frequencyLabels[frequencies[0]]
    : "Mixed schedules";
}
function groupedQueryTable(kind) {
  const groups = pageQueryGroups(kind);
  if (groups.length && !initializedQuerySections.has(kind)) {
    expandedQueries.add(groups[0].key);
    initializedQuerySections.add(kind);
  }
  return `<section class="panel query-groups"><div class="panel-head"><div><h2>${kind === "ai" ? "Questions you’re following" : "Your keyword rankings"}</h2><p class="panel-subtitle">Manage each query together. Expand it to see results by country and source.</p></div>${button(icon("plus") + " Add queries", "add-queries", "small")}</div>${
    groups
      .map((group) => {
        const open = expandedQueries.has(group.key),
          enabled = group.monitors.filter((monitor) => monitor.enabled).length,
          countries = new Set(group.monitors.map((monitor) => monitor.country)),
          sources = [
            ...new Set(group.monitors.map((monitor) => monitor.source)),
          ],
          filtered = group.visible.length !== group.monitors.length,
          checked = state.latest_observations
            .filter((row) =>
              group.monitors.some((monitor) => monitor.id === row.monitor_id),
            )
            .map((row) => row.checked_at);
        return `<section class="query-group" data-query-group="${group.id}"><div class="query-group-head"><button class="query-group-toggle" data-action="expand-query" data-id="${group.id}" aria-expanded="${open}" aria-controls="query-details-${group.id}"><span class="query-group-arrow">${icon("chevron")}</span><span><strong>${esc(group.query)}</strong><span class="query-group-meta">${countries.size} ${countries.size === 1 ? "country / region" : "countries / regions"} · ${sources.map((source) => sourceLabels[source]).join(" · ")}</span></span></button><div class="query-group-schedule"><span class="tag ${state.sync_paused || !enabled ? "neutral" : enabled === group.monitors.length ? "good" : "warn"}">${state.sync_paused ? "Sync paused" : !enabled ? "Paused" : enabled === group.monitors.length ? "Active" : `${enabled} of ${group.monitors.length} active`}</span><span>${groupSchedule(group)}</span><small>${checked.length ? `Last checked ${ago(Math.max(...checked))}` : "No checks yet"}</small></div><div class="actions query-group-actions">${button(icon("settings") + " Manage", "manage-query-group", "small", `data-id="${group.id}" aria-label="Manage ${esc(group.query)}"`)}${button(icon("refresh"), "check-query-group", "small", `data-id="${group.id}" aria-label="Check all variants of ${esc(group.query)} now" title="Check all countries and sources"`)}</div></div><div class="query-group-details" id="query-details-${group.id}" ${open ? "" : "hidden"}><div class="query-group-note">${filtered ? `Showing ${group.visible.length} of ${group.monitors.length} checks with these filters. ` : ""}Manage applies to all ${group.monitors.length} country and source checks for this query. Use the controls below for individual changes.</div>${open ? queryTable(kind, null, group.visible) : ""}</div></section>`;
      })
      .join("") ||
    empty(
      "Start following a search",
      "Add keywords for your stores or questions for AI search.",
      button("Add queries", "add-queries", "primary"),
    )
  }<div class="table-footer"><span>${groups.length} ${groups.length === 1 ? "query" : "queries"} · ${groups.reduce((count, group) => count + group.monitors.length, 0)} country and source checks</span><span>Expand a query to view results</span></div></section>`;
}
function manageQueryGroup(group) {
  const countries = [
      ...new Set(group.monitors.map((monitor) => countryName(monitor.country))),
    ],
    sources = [
      ...new Set(group.monitors.map((monitor) => sourceLabels[monitor.source])),
    ],
    apps = state.apps.filter(
      (app) =>
        !app.archived &&
        group.monitors.some((monitor) => monitor.app_ids.includes(app.id)),
    );
  showModal(
    "Manage query",
    esc(group.query),
    `<form id="query-group-form" data-ids="${group.monitors.map((monitor) => monitor.id).join(",")}"><div class="notice query-group-scope"><strong>All ${group.monitors.length} country and source checks</strong><span>${esc(countries.join(", "))}</span><span>${esc(sources.join(" · "))}</span><span>Shared by ${esc(apps.map((app) => app.name).join(", "))}. Changes apply to every app linked to these checks.</span></div><label class="field"><span>Sync schedule</span><select class="input" name="frequency"><option value="">Keep current: ${groupSchedule(group)}</option>${Object.entries(
      frequencyLabels,
    )
      .map(([key, label]) => `<option value="${key}">${label}</option>`)
      .join(
        "",
      )}</select></label><label class="field"><span>Automatic checks</span><select class="input" name="enabled"><option value="">Keep current status</option><option value="true">Resume all checks for this query</option><option value="false">Pause all checks for this query</option></select></label><p class="panel-subtitle">Country and source filters do not limit these changes. Expand the query to adjust a single check.${state.sync_paused ? " Workspace sync is paused; checks will wait until you resume it." : ""}</p><div class="form-footer"><button type="submit" class="button primary">Save query settings</button></div></form>`,
  );
}
function monitorContext(monitor) {
  if (monitor.country === "global") return "Global · Auto language";
  const parts = [countryName(monitor.country), monitor.language.toUpperCase()];
  if (monitor.source !== "google_play") {
    const device = monitor.device;
    parts.push(
      device ? device[0].toUpperCase() + device.slice(1) : "Default device",
    );
  }
  if (monitor.source === "apple_app_store")
    parts.push(`Up to ${monitor.depth * 50} results`);
  if (monitor.source === "google_play")
    parts.push(
      `Up to ${monitor.depth} ${monitor.depth === 1 ? "page" : "pages"}`,
    );
  return parts.join(" · ");
}
function queryTable(kind, limit, subset) {
  if (kind && !limit && !subset) return groupedQueryTable(kind);
  const monitors = subset || monitorRows(kind),
    rows = observationRows(kind),
    items = [];
  for (const monitor of monitors) {
    for (const target of monitor.app_ids.filter(
      (id) => !appById(id)?.archived && (!appId || id === appId),
    )) {
      const history = rows.filter(
        (r) => r.monitor_id === monitor.id && r.app_id === target,
      );
      const latest = state.latest_observations.find(
          (r) => r.monitor_id === monitor.id && r.app_id === target,
        ),
        previous = history
          .filter((r) => r.status === "success" && r.run_id !== latest?.run_id)
          .at(-1);
      items.push({ monitor, latest, previous, target });
    }
  }
  const sourceSamples = Object.keys(sourceLabels)
    .map((source) => items.find((item) => item.monitor.source === source))
    .filter(Boolean);
  const display = limit
    ? [
        ...sourceSamples,
        ...items.filter((item) => !sourceSamples.includes(item)),
      ].slice(0, limit)
    : items;
  return `${subset ? "" : `<div class="panel"><div class="panel-head"><div><h2>${kind === "ai" ? "Questions you’re following" : kind === "store" ? "Your keyword rankings" : "Your visibility trail"}</h2></div>${button(icon("plus") + " Add queries", "add-queries", "small")}</div>`}<div class="table-wrap"><table><thead><tr><th>${kind === "ai" ? "QUESTION" : "SEARCH QUERY"}</th><th>SOURCE</th><th>APP</th><th>${kind === "ai" ? "MENTION" : "VISIBILITY"}</th><th ${kind === "ai" ? 'title="Cited means a source links to this app’s store listing or configured website."' : ""}>${kind === "ai" ? "CITATION" : "CHANGE"}</th><th>LAST CHECK</th>${limit ? "" : "<th>SCHEDULE</th><th></th>"}</tr></thead><tbody>${
    display
      .map(({ monitor: m, latest: r, previous: p, target }) => {
        const store = ["apple_app_store", "google_play"].includes(m.source),
          data = r?.data || {},
          app = appById(target),
          parents = competitorParents(app).filter((parent) =>
            m.app_ids.includes(parent.id),
          ),
          rival = parents.length > 0,
          primary =
            !rival &&
            m.app_ids.some((id) => {
              const other = appById(id);
              return (
                other &&
                !other.archived &&
                other.competitor_for?.includes(target)
              );
            }),
          comparison = state.monitors.filter(
            (monitor) =>
              monitor.query === m.query &&
              monitor.app_ids.some((id) => !appById(id)?.archived),
          ),
          comparable =
            comparison.length > 1 ||
            m.app_ids.filter((id) => !appById(id)?.archived).length > 1,
          citation = data.cited
            ? '<span class="tag good">Cited</span>'
            : data.answer_available && r?.status === "success"
              ? '<span class="muted" data-tooltip="No source linked to this app’s store listing or configured website">Not cited</span>'
              : '<span class="muted">—</span>',
          context = monitorContext(m),
          controlLabel = esc(
            `${m.query} · ${sourceLabels[m.source]} · ${context}`,
          ),
          delta =
            p?.data.position && data.position
              ? p.data.position - data.position
              : null;
        let value = "—";
        if (r?.status === "error")
          value = '<span class="tag error">Check failed</span>';
        else if (r) {
          if (store)
            value = data.position
              ? `<span class="rank-number"><span>#</span>${esc(data.position)}</span>`
              : data.found
                ? '<span class="tag good">Featured</span>'
                : '<span class="tag neutral">Not found</span>';
          else
            value = !data.answer_available
              ? '<span class="tag neutral">No answer</span>'
              : data.mentioned
                ? '<span class="tag good">Mentioned</span>'
                : data.app_link
                  ? '<span class="tag good">App linked</span>'
                  : data.possible
                    ? '<span class="tag warn">Possible match</span>'
                    : '<span class="tag neutral">Not mentioned</span>';
        }
        return `<tr class="${rival ? "competitor-result" : primary ? "primary-result" : ""}"><td class="query-cell"><button class="cell-button" data-action="${r || m.latest_run_id ? "run" : "tracked-apps"}" data-id="${r?.run_id || m.latest_run_id || m.id}"><div class="query-title">${esc(m.query)}</div><div class="query-meta">${esc(context)}${!m.enabled ? " · Paused" : ""}${r?.retrospective ? " · Reanalyzed" : ""}</div></button><button class="text-link query-competitors" data-action="${m.app_ids.filter((id) => !appById(id)?.archived).length > 1 ? "tracked-apps" : "add-competitor"}" data-id="${m.id}" data-app-id="${target}" aria-label="Apps and competitors for ${esc(m.query)} on ${sourceLabels[m.source]}">${icon("plus")} ${m.app_ids.filter((id) => !appById(id)?.archived).length > 1 ? `Compare ${m.app_ids.filter((id) => !appById(id)?.archived).length} apps` : "Add competitors"}</button></td><td><span class="tag ${store ? "blue" : ""}">${sourceLabels[m.source]}</span></td><td><div class="app-inline">${appImage(app)}<span>${esc(app?.name)}${rival ? `<span class="competitor-marker" data-tooltip="${esc("Competitor for " + parents.map((parent) => parent.name).join(", "))}">Competitor</span>` : primary ? '<span class="primary-marker">Your app</span>' : ""}</span></div></td><td>${value}</td><td>${kind === "ai" ? citation : store ? `<span class="change ${delta > 0 ? "up" : delta < 0 ? "down" : ""}">${delta === null ? "—" : delta === 0 ? "No change" : `${delta > 0 ? "↑" : "↓"} ${Math.abs(delta)}`}</span>` : citation}</td><td><small class="muted">${m.latest_status === "running" || m.latest_status === "queued" ? statusTag(m.latest_status) : ago(r?.checked_at)}</small></td>${
          limit
            ? ""
            : `<td><div class="monitor-controls"><select data-frequency="${m.id}" aria-label="Frequency for ${controlLabel}">${Object.keys(
                { daily: 1, weekly: 1, biweekly: 1, monthly: 1 },
              )
                .map(
                  (f) =>
                    `<option ${m.frequency === f ? "selected" : ""}>${f}</option>`,
                )
                .join(
                  "",
                )}</select><button class="icon-button" data-action="toggle-monitor" data-id="${m.id}" data-tooltip="${m.enabled ? "Pause" : "Resume"} automatic checks" aria-label="${m.enabled ? "Pause" : "Resume"} ${controlLabel}">${icon(m.enabled ? "pause" : "play")}</button></div><div class="query-meta">${!m.enabled ? "Automatic checks paused" : state.sync_paused ? "Workspace sync paused" : "Next " + when(m.next_run_at)}</div></td><td><div class="actions query-row-actions"><button class="icon-button" data-tooltip="Check now" data-action="check-one" data-id="${m.id}" aria-label="Check ${controlLabel} now">${icon("refresh")}</button>${m.country !== "global" ? `<button class="icon-button" data-action="add-countries" data-id="${m.id}" data-tooltip="Add countries and regions" aria-label="Track ${controlLabel} in more countries">${icon("globe")}</button>` : ""}<button class="icon-button" data-action="edit-query" data-id="${m.id}" data-tooltip="Edit query" aria-label="Replace ${controlLabel}">${icon("edit")}</button>${comparable ? `<button class="icon-button" data-action="matrix" data-id="${m.id}" data-tooltip="Compare in query matrix" aria-label="Compare ${controlLabel} in query matrix">${icon("matrix")}</button>` : ""}</div></td>`
        }</tr>`;
      })
      .join("") ||
    `<tr><td colspan="8">${empty("Start following a search", "Add keywords for your stores or questions for AI search.", button("Add queries", "add-queries", "primary"))}</td></tr>`
  }</tbody></table></div><div class="table-footer"><span>${display.length} of ${items.length} tracked app/query combinations</span>${kind === "ai" ? "<span>Missing answers are excluded from mention rates.</span>" : ""}</div>${subset ? "" : "</div>"}`;
}
function competitorParents(app, includeArchived = false) {
  return (app.competitor_for || [])
    .map(appById)
    .filter((parent) => parent && (includeArchived || !parent.archived));
}
function competitorBadge(app, compact = false) {
  const parents = competitorParents(app).map((parent) => parent.name);
  return parents.length
    ? `<span class="tag competitor-tag" title="${esc("Competitor for " + parents.join(", "))}">Competitor${compact ? "" : ` · ${esc(parents.join(", "))}`}</span>`
    : "";
}
function originalForMonitor(id) {
  const monitor = state.monitors.find((item) => item.id === id);
  const apps = (monitor?.app_ids || [])
    .map(appById)
    .filter((app) => app && !app.archived);
  return (
    apps.find((app) => app.id === appId && !competitorParents(app).length)
      ?.id ||
    apps.find((app) => !competitorParents(app).length)?.id ||
    apps[0]?.id ||
    0
  );
}
function competitorSuggestion() {
  const originals = state.apps.filter(
    (app) =>
      !app.archived &&
      !competitorParents(app).length &&
      (!appId || app.id === appId),
  );
  const original = originals.find(
    (app) =>
      !state.apps.some(
        (rival) =>
          !rival.archived &&
          competitorParents(rival).some((parent) => parent.id === app.id),
      ) &&
      !state.monitors.some(
        (monitor) =>
          monitor.app_ids.includes(app.id) &&
          monitor.app_ids.some((id) => id !== app.id && !appById(id)?.archived),
      ),
  );
  if (!original) return "";
  return `<aside class="suggestion-strip competitor-suggestion">${icon("chart")}<div><strong>See how ${esc(original.name)} compares</strong><p>Add a competitor across your store queries, AI questions and countries.</p></div>${button("Add competitor " + icon("arrow"), "add-competitor", "small", `data-app-id="${original.id}"`)}</aside>`;
}
function listingHistorySuggestion() {
  if (
    state.listing_history_started ||
    state.dismissed_hints?.includes("listing-history")
  )
    return "";
  return `<aside class="suggestion-strip listing-history-suggestion">${sidebarIcon("history")}<div><strong>Follow changes to your store listings</strong><p>See how titles, descriptions, icons and screenshots change over time.</p></div><a class="text-link" href="#listing-history">Explore listing history ${icon("arrow")}</a><button class="icon-button" data-action="dismiss-listing-intro" aria-label="Dismiss listing history introduction">${icon("close")}</button></aside>`;
}
function overview() {
  const store = metric(observationRows("store"), "found"),
    ai = metric(observationRows("ai"), "mentioned"),
    citation = metric(observationRows("ai"), "cited");
  const pairs = latestPairs("store")
      .map((p) => p.latest)
      .filter((r) => r.status === "success" && r.data.position),
    average = pairs.length
      ? (pairs.reduce((n, r) => n + r.data.position, 0) / pairs.length).toFixed(
          1,
        )
      : "—";
  return (
    heading(
      "Visibility overview",
      "Review your store rankings and mentions in AI answers.",
      `<a class="button" href="/api/export.csv?${scopeParams()}">${icon("download")}Export</a>${button(icon("refresh") + " Check now", "check-all", "primary")}`,
    ) +
    setupChecklist(true) +
    filters() +
    `<div class="stats-grid">${stat("Average store position", average, `${pairs.length} ranked app/query combinations`, "chart")}${stat("Store visibility", percent(store.rate), `${store.hits} of ${store.total} successful store checks`, "eye")}${stat("AI mention rate", percent(ai.rate), `${ai.hits} mentions across ${ai.total} answers`, "sparkles")}${stat("AI citation rate", percent(citation.rate), `${citation.hits} ${citation.hits === 1 ? "answer cites" : "answers cite"} your app or website`, "globe")}</div>` +
    `<div class="chart-layout">${historyPanel()}<div class="panel"><div class="panel-head"><div><h2>Across your sources</h2></div>${icon("globe")}</div><div class="source-list">${Object.entries(
      sourceLabels,
    )
      .map(([s, l]) => {
        const rows = observationRows().filter((r) => r.params.source === s),
          m = metric(
            rows,
            ["apple_app_store", "google_play"].includes(s)
              ? "found"
              : "mentioned",
          );
        return `<div class="source-row">${sourceBadge(s)}<div class="source-info"><div class="source-name">${l}</div><div class="source-sub">${m.total ? `${m.hits} of ${m.total} checks` : "No checks in this date range"}</div></div><div><div class="source-number">${m.rate === null ? "—" : m.rate + "<small>%</small>"}</div><div class="source-meter"><span data-width="${m.rate || 0}"></span></div></div></div>`;
      })
      .join("")}</div></div></div>` +
    queryTable(null, 6) +
    competitorSuggestion() +
    listingHistorySuggestion() +
    `<div class="bottom-grid">${recentActivity()}<div class="note-card"><div class="eyebrow">FOLLOW THE NEXT QUESTION</div><h3>Does your app appear in AI answers?</h3><p>Track questions your users might ask. Review saved answers, mentions, and citations.</p>${button("Track an AI question " + icon("arrow"), "add-ai", "small")}</div></div>`
  );
}
function appCard(app) {
  const parentId = competitorParents(app)[0]?.id;
  return `<article class="panel app-card ${parentId ? "competitor-card" : ""}">${competitorBadge(app)}
    <div class="app-card-head">${appImage(app)}<div><h3>${esc(app.name)}</h3><div class="muted">${app.listings.length === 2 ? "iOS + Android" : "One connected store listing"}</div></div></div>
    <div class="app-card-listings">${app.listings.map((listing) => `<div class="listing">${sourceBadge(platformSource(listing.platform))}<div class="listing-body"><div class="listing-title"><a href="${esc(listing.url)}" target="_blank" rel="noopener noreferrer">${listing.platform === "ios" ? "App Store" : "Google Play"} ${icon("external")}</a></div><div class="listing-id">${esc(listing.external_id)}</div><div class="query-meta">${esc(listing.developer)}</div><div class="app-stats"><span>${icon("star")} ${esc(listing.metadata_json.rating ?? "—")}</span><span data-tooltip="${esc(reviewCount(listing.metadata_json.reviews, false))}">${esc(reviewCount(listing.metadata_json.reviews))}</span></div></div></div>`).join("")}</div>
    <div class="app-card-actions">${parentId ? button("Add comparisons", "add-competitor", "small", `data-app-id="${parentId}" data-existing-id="${app.id}"`) : button(icon("plus") + " Add competitor", "add-competitor", "small", `data-app-id="${app.id}"`)}${button("View trail", "select-app", "small", `data-id="${app.id}"`)}${button("Statistics", "profiles", "small", `data-id="${app.id}"`)}${button("Manage", "edit-app", "small", `data-id="${app.id}"`)}${app.listings.length < 2 ? button(icon("plus") + " Link store", "link-store", "small", `data-id="${app.id}"`) : ""}</div></article>`;
}

function appsPage() {
  if (competitors.active()) return competitors.page();
  const active = state.apps.filter((app) => !app.archived),
    archived = state.apps.filter((app) => app.archived),
    owned = active.filter((app) => !competitorParents(app).length),
    rivals = active.filter((app) => competitorParents(app).length);
  return (
    heading(
      "Apps and competitors",
      "Link your store listings and follow them together.",
      button(icon("plus") + " Add competitor", "add-competitor") +
        button(icon("plus") + " Add app", "add-app", "primary"),
      "YOUR PORTFOLIO",
    ) +
    `<section class="portfolio-section"><div class="portfolio-section-heading"><h2>Your apps</h2><span class="muted">${owned.length} ${owned.length === 1 ? "app" : "apps"}</span></div><div class="app-grid">${owned.map(appCard).join("")}<article class="panel app-add-card">${empty("Room for your next app", "Connect an app to follow its store and AI visibility.", button(icon("plus") + " Add app", "add-app", "primary"))}</article></div></section>` +
    (rivals.length
      ? `<section class="portfolio-section"><div class="portfolio-section-heading"><h2>Competitors</h2><span class="muted">${rivals.length} ${rivals.length === 1 ? "app" : "apps"}</span></div><div class="app-grid">${rivals.map(appCard).join("")}</div></section>`
      : "") +
    (archived.length
      ? `<details class="archived-apps"><summary><span class="archived-show">Show archived</span><span class="archived-hide">Hide archived</span> (${archived.length})</summary><ul class="archived-app-list">${archived.map((app) => `<li class="archived-app-row">${appImage(app)}<div class="archived-app-info"><h3>${esc(app.name)}</h3><span>${app.listings.map((listing) => (listing.platform === "ios" ? "App Store" : "Google Play")).join(" · ") || "No connected listings"}</span></div><div class="actions">${button("Manage", "edit-app", "small ghost", `data-id="${app.id}" aria-label="Manage ${esc(app.name)}"`)}${button("Restore", "restore-app", "small", `data-id="${app.id}" aria-label="Restore ${esc(app.name)}"`)}</div></li>`).join("")}</ul></details>`
      : "")
  );
}
function rankingsPage() {
  return (
    heading(
      "Store rankings",
      "Follow your app’s position for each keyword.",
      button(icon("plus") + " Add keywords", "add-store", "primary"),
      "APP STORE + GOOGLE PLAY",
    ) +
    filters() +
    queryTable("store") +
    competitorSuggestion()
  );
}
function aiPage() {
  return (
    heading(
      "AI visibility",
      "Follow your app through Google AI answers and Bing Copilot.",
      button(icon("plus") + " Add questions", "add-ai", "primary"),
      "AI SEARCH VISIBILITY",
    ) +
    filters() +
    `<div class="ai-grid">${[
      "google_ai_mode",
      "google_ai_overview",
      "bing_copilot",
    ]
      .map((source) => {
        const rows = observationRows("ai").filter(
            (r) => r.params.source === source,
          ),
          m = metric(rows, "mentioned"),
          available = rows.filter(
            (r) => r.status === "success" && r.data.answer_available,
          ).length,
          total = rows.filter((r) => r.status === "success").length;
        return `<div class="panel ai-source-card">${sourceBadge(source)}<div><h3>${sourceLabels[source]}</h3><p>${source === "bing_copilot" ? "Global results · automatic language" : "Localized questions and cited sources"}</p></div><div><div class="stat-value">${percent(m.rate)}</div><p>mention rate · ${available}/${total} answers returned</p></div></div>`;
      })
      .join("")}</div>` +
    queryTable("ai") +
    competitorSuggestion()
  );
}
function activityPage() {
  return (
    heading(
      "Search activity",
      "Review past searches and see where your app appeared.",
      button(icon("refresh") + " Check now", "check-all", "primary"),
      "SEARCH HISTORY",
    ) +
    filters() +
    `<div class="panel"><div class="panel-head"><h2>Recent checks</h2><span class="tag neutral">Latest 40 checks</span></div><div class="table-wrap"><table><thead><tr><th>SEARCH</th><th>SOURCE</th><th>STATUS</th><th>REQUESTS</th><th>STARTED</th><th></th></tr></thead><tbody>${
      dashboard.runs
        .filter((r) => !sourceFilter || r.params.source === sourceFilter)
        .map(
          (r) =>
            `<tr><td class="query-cell"><div class="query-title">${esc(r.params.query || r.params.external_id)}</div><div class="query-meta">${esc(countryName(r.params.country || "global"))}</div>${r.error ? `<div class="error-message">${esc(r.error)}</div>` : ""}</td><td>${r.kind === "listing_history" ? "Listing history" : sourceLabels[r.params.source] || "Listing refresh"}</td><td>${statusTag(r.status)}</td><td>${r.requests_count}</td><td>${when(r.started_at || r.created_at)}</td><td>${button("Inspect " + icon("arrow"), "run", "small", `data-id="${r.id}"`)}</td></tr>`,
        )
        .join("") ||
      '<tr><td colspan="6" class="table-empty">Search history will appear after your first check.</td></tr>'
    }</tbody></table></div></div>`
  );
}
function settingsPage() {
  const a = state.account?.data || {},
    estimate = state.estimated_monthly;
  return (
    heading(
      "Settings",
      "Manage your account, SerpApi connection, and backups.",
      "",
      "WORKSPACE SETTINGS",
    ) +
    `<section class="panel sync-settings" id="sync-settings" tabindex="-1"><div><div class="actions"><h2>Workspace sync</h2><span class="tag ${state.sync_paused ? "neutral" : "good"}">${state.sync_paused ? "Paused" : "Running"}</span></div><p>${state.sync_paused ? "All queued checks are on hold. Resume to continue with your saved schedules." : "Pause store rankings, AI visibility, listing history, and queued manual checks together."}</p><small class="muted">Individual schedules and pause settings are preserved. Checks already in progress may finish.</small></div>${syncToggleButton()}</section>` +
    (state.onboarding?.completed.length === 3
      ? ""
      : `<section class="panel checklist-settings"><div><h2>Setup checklist</h2><p>Reopen the shortcuts for connecting an app and adding tracking queries.</p></div>${button("Show checklist", "restore-checklist", "small")}</section>`) +
    `<div class="settings-grid"><div><section class="panel settings-panel"><h2>Your account</h2><p>Signed in as <strong>${esc(accountName)}</strong>.</p><form id="password-form"><label class="field"><span>Current password</span><input class="input" type="password" name="current_password" autocomplete="current-password" maxlength="128" required></label><label class="field"><span>New password</span><input class="input" type="password" name="new_password" autocomplete="new-password" minlength="8" maxlength="128" pattern="(?=.*[0-9])(?=.*[\\p{P}\\p{S}]).*" title="Use 8 to 128 characters, including a number and a special character." aria-describedby="new-password-help" required></label><label class="field"><span>Confirm new password</span><input class="input" type="password" name="confirm_password" autocomplete="new-password" minlength="8" maxlength="128" required></label><p id="new-password-help" class="panel-subtitle">Use 8 to 128 characters, including a number and a special character. Changing your password signs out other sessions.</p><div class="form-footer"><button class="button primary" type="submit">Change password</button></div></form></section><section class="panel settings-panel"><div class="actions"><h2>SerpApi connection</h2><span class="tag ${state.configured ? "good" : "neutral"}">${state.configured ? "Connected" : "Not connected"}</span></div><p>Connect SerpApi to check your app’s visibility.</p>${state.key_from_environment ? "" : `<form id="settings-key-form"><label class="field"><span>${state.configured ? "Replace API key" : "API key"}</span><input class="input" type="password" name="api_key" autocomplete="off" required placeholder="Your SerpApi API key"></label><div class="form-footer"><button class="button primary" type="submit">Connect and save</button></div></form>`}<a class="text-link" href="https://serpapi.com/manage-api-key" target="_blank" rel="noopener noreferrer">Find your SerpApi key ${icon("external")}</a></section><section class="panel settings-panel"><h2>Data & backups</h2><p>Download your search history or save a backup of your apps and settings.</p><div class="actions backup-actions"><a class="button" href="/api/export.csv">${icon("download")} Export history CSV</a><a class="button" href="/api/backup">${icon("download")} Download backup</a></div><p class="backup-note">Your SerpApi key is not included in backups.</p></section></div><div><section class="panel settings-panel"><div class="actions"><h2>Your search credits</h2><button class="icon-button" data-action="refresh-account" aria-label="Refresh account credits">${icon("refresh")}</button></div><div class="credit-number">${a.total_searches_left != null ? num(a.total_searches_left) : "—"}</div><div class="credit-caption">total searches remaining</div><dl class="definition"><dt>Plan</dt><dd>${esc(a.plan_name || "—")}</dd><dt>Used this month</dt><dd>${a.this_month_usage != null ? num(a.this_month_usage) : "—"}</dd><dt>Monthly allowance</dt><dd>${a.searches_per_month != null ? num(a.searches_per_month) : "—"}</dd><dt>Extra credits</dt><dd>${a.extra_credits != null ? num(a.extra_credits) : "—"}</dd><dt>Next renewal</dt><dd>${esc(a.plan_renewal_date || "Not applicable")}</dd><dt>Hourly limit</dt><dd>${a.account_rate_limit_per_hour != null ? num(a.account_rate_limit_per_hour) : "—"}</dd></dl><div class="separator"></div><div class="stat-title">Estimated AppTrail usage</div><div class="stat-value">${num(estimate.min)}${estimate.min !== estimate.max ? "–" + num(estimate.max) : ""}<small>/mo</small></div><p class="panel-subtitle">${state.sync_paused ? "Estimate for when workspace sync resumes. " : ""}Based on active queries and enabled listing histories. Discovery, retries, and manual checks are additional.</p><div class="separator"></div><small class="muted">Account-wide balance · Updated ${ago(state.account?.checked_at)}</small></section></div></div>`
  );
}
function welcome() {
  const sources = (items) =>
    `<ul class="welcome-sources">${items.map((source) => `<li>${sourceBadge(source)}<span>${sourceLabels[source]}</span></li>`).join("")}</ul>`;
  return `<div class="welcome">
    <div class="welcome-start ${state.onboarding?.visible ? "" : "without-checklist"}">
      <div class="welcome-intro">
        <div class="eyebrow">WELCOME TO APPTRAIL</div>
        <h1>Track your app’s visibility.</h1>
        <p>See where your app ranks in stores and whether AI answers mention it. Follow the searches that matter to you, across the countries you choose.</p>
        ${button((state.configured ? "Add your first app" : "Set up AppTrail") + " " + icon("arrow"), "setup", "primary")}
      </div>
      ${setupChecklist()}
    </div>
    <div class="welcome-features">
      <section class="panel welcome-feature"><h2>App store rankings</h2><p>Follow keyword positions by country and compare your app with competitors.</p>${sources(["apple_app_store", "google_play"])}</section>
      <section class="panel welcome-feature"><h2>AI search visibility</h2><p>Track mentions and citations in answers to the questions your users ask.</p>${sources(["google_ai_mode", "google_ai_overview", "bing_copilot"])}</section>
    </div>
  </div>`;
}
const compactNavigation = window.matchMedia("(max-width: 900px)");
function setNavigation(open, restoreFocus = false) {
  open = open && compactNavigation.matches;
  const sidebar = $("#sidebar");
  sidebar.classList.toggle("open", open);
  sidebar.inert = compactNavigation.matches && !open;
  $(".main-shell").inert = open;
  $(".navigation-backdrop").hidden = !open;
  $(".mobile-menu").setAttribute("aria-expanded", String(open));
  document.body.classList.toggle("navigation-open", open);
  if (open) $(".sidebar-close").focus();
  else if (restoreFocus && compactNavigation.matches) $(".mobile-menu").focus();
}
compactNavigation.addEventListener("change", () => setNavigation(false));
document.addEventListener("keydown", (event) => {
  if (!$("#sidebar").classList.contains("open")) return;
  if (event.key === "Escape") {
    event.preventDefault();
    setNavigation(false, true);
  } else if (event.key === "Tab") {
    const links = $$("a[href], button:not([disabled])", $("#sidebar"));
    const first = links[0],
      last = links.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
});
setNavigation(false);
const frequencyLabels = {
  daily: "Daily",
  weekly: "Weekly",
  biweekly: "Every two weeks",
  monthly: "Monthly",
};
const usageRange = (usage) =>
  num(usage.min) + (usage.min !== usage.max ? "–" + num(usage.max) : "");
function syncToggleButton() {
  const paused = trackingState?.sync_paused;
  return button(
    icon(paused ? "play" : "pause") +
      (paused ? " Resume all sync" : " Pause all sync"),
    "toggle-sync",
    "small",
    "data-sync-toggle",
  );
}
function progressCopy() {
  if (trackingUnavailable)
    return {
      title: "Search progress unavailable",
      detail: "Reconnecting to AppTrail…",
    };
  if (trackingState?.sync_paused)
    return {
      title: "Workspace sync paused",
      detail: `${num(progress?.queued || 0)} checks on hold${progress?.running ? ` · ${num(progress.running)} already in progress` : ""}. Resume in Settings or Search schedule.`,
    };
  if (!progress?.pending) {
    if (!progress) return { title: "No checks in progress", detail: "" };
    return {
      title: progress.failed
        ? "Checks finished with errors"
        : progress.succeeded
          ? "Searches updated"
          : "Checks stopped",
      detail: [
        progress.succeeded ? `${num(progress.succeeded)} completed` : "",
        progress.failed ? `${num(progress.failed)} failed` : "",
        progress.cancelled ? `${num(progress.cancelled)} cancelled` : "",
      ]
        .filter(Boolean)
        .join(" · "),
    };
  }
  const current = trackingState?.current_job;
  const waiting =
    !progress.running && current?.available_at > Date.now() / 1000;
  return {
    title: !trackingState?.configured
      ? "Searches waiting for SerpApi"
      : waiting
        ? "Waiting to retry"
        : progress.running
          ? "Checks in progress"
          : "Checks queued",
    detail: [
      `${num(progress.running)} running`,
      `${num(progress.queued)} queued`,
      `${num(progress.completed)} of ${num(progress.total)} finished`,
      progress.failed ? `${num(progress.failed)} failed` : "",
      !trackingState?.configured
        ? "Connect your key in Settings"
        : waiting
          ? `Next attempt ${when(current.available_at)}`
          : "",
    ]
      .filter(Boolean)
      .join(" · "),
  };
}
function scheduledCheck(query) {
  if (query.latest_status === "running") return "Running now";
  if (trackingState?.sync_paused) return "Waiting for workspace sync to resume";
  if (query.latest_status === "queued") return "Queued";
  return query.next_run_at <= Date.now() / 1000
    ? "Due now"
    : `Next due ${when(query.next_run_at)}`;
}
function renderSearchProgress() {
  if (!trackingState || leaving) return;
  const summary = trackingSummary(trackingState.monitors, trackingState.apps);
  $("#query-count").textContent =
    `${num(summary.active.length)} active ${summary.active.length === 1 ? "query" : "queries"}${trackingState.sync_paused ? " · Sync paused" : ""}`;
  for (const element of $$("[data-sync-toggle]")) {
    const paused = trackingState.sync_paused;
    element.innerHTML =
      icon(paused ? "play" : "pause") +
      (paused ? " Resume all sync" : " Pause all sync");
  }
  $("#credit-mini").title = "View search schedule and estimated usage";
  const region = $("#search-progress"),
    copy = progressCopy();
  const action = $("#search-progress-action"),
    paused = trackingState.sync_paused;
  action.href = paused ? "#settings" : "#activity";
  action.dataset.action = paused ? "sync-settings" : "summary-route";
  action.dataset.route = paused ? "settings" : "activity";
  $("#search-progress-action-label").textContent = paused
    ? "Open settings"
    : "View activity";
  region.hidden =
    !trackingState.sync_paused &&
    (!progress || (!progress.pending && progressDismissed));
  region.classList.toggle(
    "is-running",
    Boolean(
      progress?.pending && !trackingUnavailable && !trackingState.sync_paused,
    ),
  );
  region.classList.toggle(
    "has-errors",
    Boolean(progress?.failed || trackingUnavailable),
  );
  const symbol = trackingState.sync_paused
    ? "pause"
    : progress?.pending
      ? "refresh"
      : progress?.failed
        ? "eye"
        : "check";
  const mark = $(".search-progress-icon", region);
  if (mark.dataset.symbol !== symbol) {
    mark.innerHTML = icon(symbol);
    mark.dataset.symbol = symbol;
  }
  for (const [id, value] of [
    ["search-progress-title", copy.title],
    ["search-progress-detail", copy.detail],
  ]) {
    if ($("#" + id).textContent !== value) $("#" + id).textContent = value;
  }
  $("[data-action='dismiss-progress']", region).hidden = Boolean(
    progress?.pending || trackingState.sync_paused,
  );
  for (const element of $$("[data-live-search-status]")) {
    const text = [copy.title, copy.detail].filter(Boolean).join(" · ");
    if (element.textContent !== text) element.textContent = text;
  }
  const monitors = new Map(
    trackingState.monitors.map((query) => [query.id, query]),
  );
  for (const element of $$("[data-scheduled-monitor]")) {
    const query = monitors.get(Number(element.dataset.scheduledMonitor));
    if (query && element.textContent !== scheduledCheck(query))
      element.textContent = scheduledCheck(query);
  }
}
function updateSearchProgress(newState) {
  const wasPending = progress?.pending;
  trackingState = newState;
  trackingUnavailable = false;
  progress = searchProgress(progress, newState.jobs);
  if (progress?.pending && !wasPending) progressDismissed = false;
  renderSearchProgress();
}
function showSearchSummary() {
  if (!trackingState) return;
  setNavigation(false);
  const summary = trackingSummary(trackingState.monitors, trackingState.apps);
  const listingCredits = trackingState.listing_monthly_credits || 0;
  const hasListingHistory =
    trackingState.listing_history_started || listingCredits > 0;
  const listingRow = hasListingHistory
    ? `<a class="search-source search-source-link" href="#listing-history" data-action="summary-route" data-route="listing-history"><span class="source-symbol" aria-hidden="true">${icon("clock")}</span><span class="search-source-name"><strong>Listing history</strong><small>${listingCredits ? "Store listing snapshots" : "Collection paused"}</small></span><strong class="search-source-usage">${num(Math.round(listingCredits))}</strong><span class="search-source-arrow">${icon("chevron")}</span></a>`
    : "";
  const frequencies = Object.entries(frequencyLabels)
    .map(([key, label]) => {
      const count = summary.active.filter(
        (query) => query.frequency === key,
      ).length;
      return count ? `${num(count)} ${label.toLowerCase()}` : "";
    })
    .filter(Boolean)
    .join(" · ");
  const total = `<div class="search-usage-total"><div><span>${trackingState.sync_paused ? "Estimated total searches / month when resumed" : "Estimated total searches / month"}</span><strong>${usageRange(trackingState.estimated_monthly)}</strong></div>${frequencies ? `<p>Queries: ${frequencies}</p>` : ""}</div>
    <p class="search-summary-live" data-live-search-status role="status" aria-live="polite"></p>`;
  const body = summary.active.length || hasListingHistory
    ? `
    <div class="search-source-heading"><span>Tracking source</span><span>Searches / month</span></div>
    <div class="search-source-list">${Object.keys(sourceLabels)
      .filter((source) =>
        summary.groups.some((group) => group.source === source),
      )
      .map((source) => {
        const group = summary.groups.find((item) => item.source === source);
        return `<details class="search-source"><summary>${sourceBadge(source)}<span class="search-source-name"><strong>${sourceLabels[source]}</strong><small>${num(group.queries.length)} ${group.queries.length === 1 ? "query" : "queries"}</small></span><strong class="search-source-usage">${usageRange(group.usage)}</strong><span class="search-source-arrow">${icon("chevron")}</span></summary><ul>${group.queries
          .slice(0, 25)
          .map((query) => {
            const calls =
              source === "google_play" && query.depth > 1
                ? `Up to ${query.depth} requests / check`
                : source === "google_ai_overview"
                  ? "1–2 requests / check"
                  : "1 request / check";
            return `<li><strong>${esc(query.query)}</strong><span>${esc(countryName(query.country))} · ${frequencyLabels[query.frequency]} · ${calls}</span><small data-scheduled-monitor="${query.id}">${esc(scheduledCheck(query))}</small></li>`;
          })
          .join(
            "",
          )}</ul>${group.queries.length > 25 ? `<p class="search-more">${num(group.queries.length - 25)} more queries. Open ${source.startsWith("google_ai") || source === "bing_copilot" ? "AI visibility" : "Store rankings"} to view all.</p>` : ""}</details>`;
      })
      .join("")}${listingRow}</div>
    <p class="search-summary-note">30-day estimate for queries and listing history at your saved schedules and search depth. Google Play can fetch multiple pages; AI Overviews may need a follow-up request. Discovery, retries, and manual checks are extra.</p>
  `
    : `<div class="empty"><h3>No active queries</h3><p>Add keywords or AI questions, or resume a paused query.</p>${button("Add queries", "add-queries", "primary")}</div>`;
  showModal(
    "Search schedule",
    "Schedules and estimated usage across your workspace.",
    total +
      body +
      `<div class="search-sync-shortcuts"><div><strong>Workspace sync</strong><p class="panel-subtitle">Pause every sync job while keeping individual schedules.</p></div><div class="actions">${syncToggleButton()}${button("Sync settings " + icon("arrow"), "sync-settings", "small")}</div></div>`,
    `<div class="actions search-summary-links">${button("Store rankings", "summary-route", "small", 'data-route="rankings"')}${button("AI visibility", "summary-route", "small", 'data-route="ai"')}${button("Listing history", "summary-route", "small", 'data-route="listing-history"')}${button("View activity", "summary-route", "small", 'data-route="activity"')}</div>`,
  );
  modal.dataset.view = "search-summary";
  renderSearchProgress();
}
function render() {
  $("#credit-mini").disabled = false;
  charts.forEach((c) => c.destroy());
  charts = [];
  $$("[data-nav]").forEach((a) =>
    a.classList.toggle("active", a.dataset.nav === route()),
  );
  $("#breadcrumb").textContent = labels[route()];
  const unread = state.notification_unread || 0,
    notificationBell = $("#notification-bell");
  notificationBell.disabled = isLoading(notificationBell);
  $("#notification-count").textContent = unread > 99 ? "99+" : unread;
  $("#notification-count").hidden = !unread;
  notificationBell.setAttribute(
    "aria-label",
    `Notifications${unread ? `, ${unread} unread` : ""}`,
  );
  $("#app-count").textContent = state.apps.filter((a) => !a.archived).length;
  $("#version-label").textContent = `OPEN SOURCE · v${state.version}`;
  $("#main").innerHTML =
    !state.apps.length && route() !== "settings"
      ? welcome()
      : {
          overview,
          apps: appsPage,
          rankings: rankingsPage,
          ai: aiPage,
          activity: activityPage,
          "listing-history": insights.listingPage,
          settings: settingsPage,
        }[route()]();
  $$("[data-width]").forEach(
    (el) => (el.style.width = `${Number(el.dataset.width)}%`),
  );
  $$("[data-color]").forEach(
    (el) => (el.style.background = sourceColors[el.dataset.color]),
  );
  if ($("#trend-chart")) drawChart();
  $$("[data-flex]").forEach((el) => {
    el.style.flexGrow = Number(el.dataset.flex);
  });
  $$(".table-wrap").forEach((table) => {
    table.tabIndex = 0;
    table.setAttribute("role", "region");
    table.setAttribute(
      "aria-label",
      "Results table. Scroll horizontally for more columns.",
    );
  });
  competitors.updateSummary();
}
function drawChart() {
  const canvas = $("#trend-chart");
  if (!window.Chart) {
    canvas.parentElement.innerHTML =
      '<div class="chart-fallback">The chart is unavailable. You can still view your results in the table below.</div>';
    return;
  }
  const rank = chartMode === "rank",
    options = chartOptions(),
    comparing = chartGroup === "country",
    { dates, series } = visibilitySeries(
      observationRows(rank ? "store" : "ai"),
      {
        kind: chartMode,
        groupBy: chartGroup,
        source: comparing ? comparisonSource : "",
        query: comparing ? comparisonQuery : "",
        countries: options.selected,
      },
    );
  if (!dates.length) {
    canvas.parentElement.innerHTML = `<div class="charts-empty"><strong>${comparing && !options.selected.length ? "Choose countries to compare." : "No checks in this date range."}</strong></div>`;
    return;
  }
  const palette = [
    "#6937ea",
    "#245fbb",
    "#28734c",
    "#a6530d",
    "#ad373d",
    "#8e3e8d",
    "#167385",
    "#765b38",
  ];
  const datasets = series.map(({ key, data }) => {
    const index = options.countries.indexOf(key),
      color = comparing ? palette[index % palette.length] : sourceColors[key];
    return {
      label: comparing ? countryName(key) : sourceLabels[key],
      data,
      borderColor: color,
      backgroundColor: color,
      borderDash: comparing && index >= palette.length ? [5, 3] : [],
      borderWidth: 2,
      pointRadius: dates.length === 1 ? 5 : 3,
      pointHoverRadius: 6,
      clip: 8,
      tension: 0.25,
      spanGaps: false,
    };
  });
  const legend = $("#trend-legend");
  legend.innerHTML = datasets
    .map(
      (dataset) =>
        `<span class="legend-item"><span class="legend-dot"></span>${esc(dataset.label)}</span>`,
    )
    .join("");
  $$(".legend-dot", legend).forEach((dot, i) => {
    dot.style.backgroundColor = datasets[i].borderColor;
  });
  charts.push(
    new Chart(canvas, {
      type: "line",
      data: {
        labels: dates.map((d) =>
          new Date(d + "T12:00:00").toLocaleDateString(undefined, {
            month: "short",
            day: "numeric",
          }),
        ),
        datasets,
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        layout: { padding: 8 },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { backgroundColor: "#313131", padding: 12 },
        },
        scales: {
          x: {
            offset: dates.length === 1,
            grid: { display: false },
            ticks: { font: { size: 9 }, color: "#656570" },
            border: { display: false },
          },
          y: {
            reverse: rank,
            min: rank ? 1 : 0,
            max: rank ? undefined : 100,
            grid: { color: "#e3e5ea" },
            border: { display: false },
            ticks: {
              font: { size: 9 },
              color: "#656570",
              precision: 0,
              callback: (v) => (rank ? "#" + v : v + "%"),
            },
          },
        },
      },
    }),
  );
}
const dirtyForms = new WeakSet();
let syncPending = 0;
let dashboardRefreshedAt = 0;
function editingMain() {
  const main = $("#main");
  return (
    (main.contains(document.activeElement) &&
      document.activeElement.matches("input, select, textarea")) ||
    Boolean($("details[open]", main)) ||
    $$("form", main).some((form) => dirtyForms.has(form))
  );
}
document.addEventListener("input", (event) => {
  if (event.target.form) dirtyForms.add(event.target.form);
});
document.addEventListener("reset", (event) => dirtyForms.delete(event.target));
async function sync(silent = false) {
  if (leaving || (silent && syncPending)) return;
  const finishLoading = silent ? null : beginLoading();
  syncPending++;
  if (!silent) renderRequested = true;
  const version = ++syncVersion;
  try {
    const newState = await api("state");
    if (leaving || version !== syncVersion) return;
    updateSearchProgress(newState);
    if (silent && editingMain()) return;
    const stateChanged = JSON.stringify(state) !== JSON.stringify(newState);
    if (silent && !stateChanged && Date.now() - dashboardRefreshedAt < 60000)
      return;
    if (sourceFilter && !sourceInView(sourceFilter)) sourceFilter = "";
    if (appId && !newState.apps.some((a) => a.id === appId && !a.archived))
      appId = 0;
    if (
      route() !== "listing-history" &&
      countryFilter &&
      !availableCountries(newState).includes(countryFilter)
    )
      countryFilter = "";
    const params = scopeParams();
    params.set("days", String(days || 30));
    if (days === 0 && customStart)
      params.set(
        "start",
        String(new Date(customStart + "T00:00:00").getTime() / 1000),
      );
    if (days === 0 && customEnd)
      params.set(
        "end",
        String(new Date(customEnd + "T23:59:59").getTime() / 1000),
      );
    const newDashboard = await api("dashboard?" + params);
    const insightData = await insights.load(
      route(),
      params,
      chartView === "distribution",
    );
    if (leaving) return;
    if (version !== syncVersion || (silent && editingMain())) return;
    const changed =
      stateChanged ||
      JSON.stringify(dashboard) !== JSON.stringify(newDashboard);
    state = newState;
    dashboard = newDashboard;
    dashboardRefreshedAt = Date.now();
    insights.apply(insightData);
    if (renderRequested || changed) {
      renderRequested = false;
      render();
    }
  } catch (error) {
    trackingUnavailable = true;
    renderSearchProgress();
    if (!silent) toast(error.message, true);
  } finally {
    syncPending--;
    finishLoading?.();
  }
}
function showModal(title, subtitle, body, footer = "") {
  delete modal.dataset.monitorId;
  delete modal.dataset.view;
  modal.innerHTML = `<div class="modal-header"><div><h2 id="modal-title">${title}</h2>${subtitle ? `<p>${subtitle}</p>` : ""}</div><button class="icon-button" data-action="close-modal" aria-label="Close dialog">${icon("close")}</button></div><div class="modal-body">${body}<div id="modal-error" class="error-message" role="alert"></div></div>${footer ? `<div class="modal-footer">${footer}</div>` : ""}`;
  if (!modal.open) modal.showModal();
  placeLoadingIndicator();
  updateCountryPicker();
  estimateForm();
}
function steps() {
  return `<div class="steps">${["Connect", "Your app", "Queries", "Start tracking"].map((label, i) => `${i ? '<div class="step-line"></div>' : ""}<div class="step ${wizard.step === i + 1 ? "active" : wizard.step > i + 1 ? "done" : ""}"><span>${wizard.step > i + 1 ? icon("check") : i + 1}</span>${label}</div>`).join("")}</div>`;
}
function startWizard(app = null, monitor = null) {
  wizard = {
    step: state.configured ? 2 : 1,
    appId: app?.id || null,
    linkOnly: !!app,
    monitorId: monitor?.id || null,
    name: app?.name || "",
    website: app?.website || "",
    aliases: app?.aliases.join(", ") || "",
    platform: app?.listings.some((l) => l.platform === "ios")
      ? "android"
      : "ios",
    country: "us",
    language: "en",
    query: "",
  };
  if (monitor) {
    wizard.platform = monitorPlatform(monitor) || wizard.platform;
    wizard.country = monitor.country === "global" ? "us" : monitor.country;
    wizard.language = monitor.language === "auto" ? "en" : monitor.language;
  }
  candidates = [];
  selectedCandidates = {};
  renderWizard();
}
function rememberWizard() {
  if (!wizard) return;
  for (const key of [
    "name",
    "website",
    "aliases",
    "query",
    "country",
    "language",
  ]) {
    const el = $(`[data-wizard-field="${key}"]`);
    if (el) wizard[key] = el.value;
  }
}
function wizardSaveLabel() {
  return wizard.monitorId
    ? "Connect & add to search"
    : Object.keys(selectedCandidates).length > 1
      ? "Connect listings"
      : "Connect listing";
}
function restoreWizardQueries() {
  const draft = wizard.queryDraft,
    form = $("#query-form");
  if (!draft || !form) return;
  for (const input of form.elements) {
    if (!input.name) continue;
    if (input.type === "checkbox") {
      if (input.name !== "sources" || draft.sources.includes(input.value))
        input.checked = draft.values.getAll(input.name).includes(input.value);
    } else if (draft.values.has(input.name))
      input.value = draft.values.get(input.name);
  }
  updateCountryPicker();
  estimateForm();
}
function renderWizard() {
  let body = wizard.monitorId ? "" : steps(),
    footer = "";
  if (wizard.step === 1) {
    body += `<div class="connect-logo">${icon("key")}</div><h3>Connect your SerpApi account</h3><form id="wizard-key-form"><label class="field"><span>SerpApi API key</span><input class="input" name="api_key" type="password" autocomplete="off" placeholder="Paste your API key" required></label><a class="text-link" href="https://serpapi.com/manage-api-key" target="_blank" rel="noopener noreferrer">Get your API key ${icon("external")}</a><div class="form-footer"><button class="button primary" type="submit">Connect account ${icon("arrow")}</button></div></form>`;
  }
  if (wizard.step === 2) {
    const storeCountries = sourceCountries(platformSource(wizard.platform));
    if (!storeCountries.includes(wizard.country)) wizard.country = "us";
    const connected = appById(wizard.appId)?.listings || [],
      picked = Object.values(selectedCandidates),
      requiredPlatform = wizard.monitorId
        ? monitorPlatform(state.monitors.find((m) => m.id === wizard.monitorId))
        : null,
      available = ["ios", "android"].filter(
        (platform) =>
          (!requiredPlatform || requiredPlatform === platform) &&
          !connected.some((listing) => listing.platform === platform),
      );
    body += connected.length
      ? `<div class="connected-listings">${connected.map((listing) => `<div class="connected-listing">${sourceBadge(platformSource(listing.platform))}<div><strong>${esc(listing.title)}</strong><small>${esc(listing.external_id)}</small></div><span class="tag good">Connected</span></div>`).join("")}</div>`
      : "";
    if (available.length) {
      if (connected.length === 1 && !wizard.linkOnly)
        body += `<h3 class="store-connect-title">Connect ${available[0] === "ios" ? "App Store" : "Google Play"} <span class="muted">(optional)</span></h3>`;
      body += `${available.length > 1 ? `<div class="modal-tabs">${available.map((platform) => `<button class="chip ${wizard.platform === platform ? "active" : ""}" data-action="wizard-platform" data-platform="${platform}">${platform === "ios" ? "Apple App Store" : "Google Play"}</button>`).join("")}</div>` : ""}<form id="discovery-form"><div class="inline-form"><input class="input" name="query" data-wizard-field="query" value="${esc(wizard.query)}" required placeholder="Search by app name or paste a store URL" aria-label="App name or store URL"><button class="button primary" type="submit">${icon("search")} Search</button></div><div class="field-row"><label class="field"><span>Country</span><select name="country" data-wizard-field="country">${countries(wizard.country, storeCountries)}</select></label><label class="field"><span>Language</span><select name="language" data-wizard-field="language">${languages(wizard.language)}</select></label></div></form><div class="candidates">${candidates.map((candidate, i) => `<button class="candidate ${selectedCandidates[wizard.platform]?.token === candidate.token ? "selected" : ""}" data-action="select-candidate" data-index="${i}"><img src="${esc(candidate.icon)}" alt=""><span><strong>${esc(candidate.title)}</strong><small>${esc(candidate.developer)}</small><small>${esc(candidate.external_id)}</small></span></button>`).join("")}</div><div class="selected-listings">${picked.map((candidate) => `<span class="tag good">${icon("check")} ${candidate.platform === "ios" ? "App Store" : "Google Play"}: ${esc(candidate.title)}</span>`).join("")}</div>`;
    }
    if (!wizard.appId)
      body += `<div class="field"><label class="field-label" for="wizard-app-name">App name in your workspace</label><input class="input" id="wizard-app-name" data-wizard-field="name" value="${esc(wizard.name)}" placeholder="e.g. Todoist" maxlength="160" aria-describedby="wizard-app-name-help"><small id="wizard-app-name-help">AI matching uses this name and your aliases. Include the short product name if your workspace label or store title contains extra words.</small></div><div class="field-row"><label class="field"><span>Other names (optional)</span><input class="input" data-wizard-field="aliases" value="${esc(wizard.aliases)}" placeholder="Comma-separated aliases"></label><label class="field"><span>App website (optional)</span><input class="input" data-wizard-field="website" value="${esc(wizard.website)}" placeholder="https://yourapp.com" aria-describedby="wizard-website-help"><small id="wizard-website-help">Used to track AI citations linking to your app’s website.</small></label></div>`;
    const canContinue =
      connected.length && !wizard.linkOnly && !wizard.monitorId;
    footer = `${wizard.monitorId ? button("Back to comparison", "tracked-apps", "", `data-id="${wizard.monitorId}"`) : "<small>Searches use your SerpApi credits.</small>"}<div class="actions wizard-actions">${canContinue ? button(connected.length === 1 ? "Continue with one store" : "Continue to queries", "wizard-queries", picked.length ? "" : "primary") : ""}${!canContinue || picked.length ? button(wizardSaveLabel(), "save-wizard-app", "primary", picked.length ? "" : "disabled") : ""}</div>`;
  }
  if (wizard.step === 3) {
    body += queryForm(wizard.appId, "all", true);
  }
  if (wizard.step === 4) {
    body += `<div class="onboard-success"><div class="connect-logo">${icon("check")}</div><h3>Your tracking is set up.</h3><p data-live-search-status role="status" aria-live="polite"></p><div class="notice">Keep AppTrail running for automatic updates.</div></div>`;
    footer = `<small>You can add more apps and queries any time.</small>${button("Open my dashboard " + icon("arrow"), "finish-wizard", "primary")}`;
  }
  showModal(
    wizard.monitorId
      ? "Add a competitor"
      : wizard.linkOnly
        ? "Link another store"
        : "Set up tracking",
    wizard.monitorId
      ? "Finding and connecting a store listing uses SerpApi credits."
      : wizard.linkOnly
        ? "Connect the other store listing to this app."
        : "Choose an app and add the searches to track.",
    body,
    footer,
  );
  if (wizard.step === 3) restoreWizardQueries();
  if (wizard.step === 4) renderSearchProgress();
}
const sourceCountries = (source) =>
  regions[source] || regions[source === "apple_app_store" ? "apple" : "google"];
const countries = (selected, available) =>
  Object.entries(regions.countries)
    .filter(([code]) => available.includes(code))
    .map(
      ([v, l]) =>
        `<option value="${v}" ${selected === v ? "selected" : ""}>${esc(l)}</option>`,
    )
    .join("");
function countryPicker(selected = ["us"]) {
  return `<details class="country-picker"><summary><span>Countries</span><span class="country-summary"></span></summary><div class="country-picker-body"><label class="visually-hidden" for="country-search">Find countries</label><input class="input" id="country-search" type="search" placeholder="Find a country or code" autocomplete="off"><div class="country-toolbar"><button class="text-link country-count" type="button" data-action="selected-countries" aria-pressed="false" title="Show selected countries"></button><div class="actions"><button class="text-link" type="button" data-action="select-countries">Select all</button><button class="text-link" type="button" data-action="clear-countries">Clear</button></div></div><div class="country-options" role="group" aria-label="Countries to track">${Object.entries(
    regions.countries,
  )
    .map(
      ([code, name]) =>
        `<label class="country-option" data-country="${code}" data-name="${esc(name.toLowerCase())}"><input type="checkbox" name="countries" value="${code}" ${selected.includes(code) ? "checked" : ""}><span>${esc(name)}</span><small>${code.toUpperCase()}</small></label>`,
    )
    .join(
      "",
    )}</div><p class="country-empty" hidden>No matching countries.</p></div></details><p class="source-note country-availability">Available countries depend on the selected sources. Each country uses separate search credits.</p>`;
}
function updateCountryPicker() {
  const form = $("#query-form") || $("#country-form");
  if (!form) return;
  const sources = new FormData(form).getAll("sources"),
    regional = sources.filter((s) => s !== "bing_copilot"),
    query = ($("#country-search", form)?.value || "").trim().toLowerCase(),
    selectedOnly =
      $(".country-count", form).getAttribute("aria-pressed") === "true";
  let visible = 0;
  $$(".country-option", form).forEach((row) => {
    const input = $("input", row);
    const supported =
      regional.length > 0 &&
      regional.every((source) => sourceCountries(source).includes(input.value));
    input.disabled = !supported;
    row.hidden =
      !supported ||
      (selectedOnly && !input.checked) ||
      !(row.dataset.name.includes(query) || input.value.includes(query));
    if (!row.hidden) visible++;
  });
  const selected = $$('input[name="countries"]:checked:not(:disabled)', form);
  $(".country-summary", form).textContent =
    selected.length === 1
      ? countryName(selected[0].value)
      : `${selected.length} selected`;
  $(".country-count", form).textContent = `${selected.length} selected`;
  $(".country-empty", form).hidden = visible > 0;
  const picker = $(".country-picker", form);
  picker.hidden = regional.length === 0;
  $(".country-availability", form).hidden = regional.length === 0;
  $('[data-action="select-countries"]', form).textContent = query
    ? "Select results"
    : "Select all";
  const language = $('[name="language"]', form);
  if (language) {
    language.disabled = regional.length === 0;
    language.closest("label").hidden = regional.length === 0;
  }
  const device = $('[name="device"]', form);
  if (device) {
    const enabled = sources.some((s) =>
      ["apple_app_store", "google_ai_mode", "google_ai_overview"].includes(s),
    );
    device.disabled = !enabled;
    device.closest("label").hidden = !enabled;
  }
  const depth = $('[name="depth"]', form);
  if (depth) {
    const enabled = sources.some((s) =>
      ["apple_app_store", "google_play"].includes(s),
    );
    depth.disabled = !enabled;
    depth.closest("label").hidden = !enabled;
  }
  const advanced = $(".query-advanced", form);
  if (advanced)
    advanced.hidden =
      (!device || device.disabled) && (!depth || depth.disabled);
}
function formMonitors(form) {
  const data = new FormData(form);
  if (form.id === "country-form") {
    const old = state.monitors.find((m) => m.id === Number(form.dataset.id));
    return expandQueries({
      sources: [old.source],
      countries: data.getAll("countries"),
      storeQueries: [old.query],
      aiQueries: [old.query],
      language: old.language,
      device: old.device,
      frequency: old.frequency,
      depth: old.depth,
      appIds: old.app_ids.filter((id) => !appById(id)?.archived),
    });
  }
  return expandQueries({
    sources: data.getAll("sources"),
    countries: data.getAll("countries"),
    storeQueries: (data.get("store_queries") || "").split("\n"),
    aiQueries: (data.get("ai_queries") || "").split("\n"),
    language: data.get("language") || "en",
    frequency: data.get("frequency"),
    device: data.get("device"),
    depth: data.get("depth"),
    appIds: [Number(data.get("app_id") || form.dataset.appId)],
  });
}
function addCountries(id) {
  const monitor = state.monitors.find((m) => m.id === id);
  showModal(
    "Track in more countries",
    `${esc(monitor.query)} · ${sourceLabels[monitor.source]}`,
    `<form id="country-form" data-id="${id}"><input type="hidden" name="sources" value="${monitor.source}">${countryPicker([])}<p class="source-note">Uses the same language, device, schedule, and ${monitor.app_ids.length} linked ${monitor.app_ids.length === 1 ? "app" : "apps"}.</p><div class="notice" id="query-estimate"></div><div class="form-footer"><button class="button primary" type="submit">Add countries & check</button></div></form>`,
  );
  $(".country-picker").open = true;
}
const languages = (selected) =>
  [
    ["en", "English"],
    ["de", "German"],
    ["fr", "French"],
    ["es", "Spanish"],
    ["ja", "Japanese"],
    ["pt", "Portuguese"],
    ["hi", "Hindi"],
  ]
    .map(
      ([v, l]) =>
        `<option value="${v}" ${selected === v ? "selected" : ""}>${l}</option>`,
    )
    .join("");
function queryForm(id, kind = "all", inWizard = false) {
  const app = appById(id) || state.apps.find((a) => !a.archived),
    platforms = app?.listings.map((l) => l.platform) || [];
  return `<form id="query-form" data-kind="${kind}" data-app-id="${app?.id || ""}" data-wizard="${inWizard}">${
    inWizard
      ? ""
      : `<label class="field"><span>Track this app</span><select id="query-app" name="app_id">${state.apps
          .filter((a) => !a.archived)
          .map(
            (a) =>
              `<option value="${a.id}" ${a.id === app?.id ? "selected" : ""}>${esc(a.name)}</option>`,
          )
          .join("")}</select></label>`
  }${kind !== "ai" ? `<label class="field"><span>Store keywords · one per line</span><textarea name="store_queries" placeholder="habit tracker\ndaily planner\nto do list"></textarea></label><div class="check-grid">${platforms.map((p) => `<label class="check-chip"><input type="checkbox" name="sources" value="${platformSource(p)}" checked>${p === "ios" ? "App Store" : "Google Play"}</label>`).join("")}</div>` : ""}${kind !== "store" ? `<label class="field"><span>AI questions · one per line</span><textarea name="ai_queries" placeholder="Which is the best iOS app for …?\nWhat Android apps help with …?"></textarea><small>Ask what your users would ask. Your app name does not need to be in the question.</small></label><div class="suggestions">${["Which mobile app is best for managing daily tasks?", "Suggest Android apps to help me build better habits.", "What are the best offline apps for personal productivity?"].map((s) => `<button type="button" class="suggestion" data-action="suggest-question" data-text="${esc(s)}">${icon("plus")} ${esc(s)}</button>`).join("")}</div><div class="check-grid">${["google_ai_mode", "google_ai_overview", "bing_copilot"].map((s) => `<label class="check-chip"><input type="checkbox" name="sources" value="${s}" checked>${sourceLabels[s]}</label>`).join("")}</div><p class="source-note">Bing Copilot uses global results and automatic language. Other sources use the selected countries.</p>` : ""}${countryPicker(countryFilter && countryFilter !== "global" ? [countryFilter] : ["us"])}<div class="field-row"><label class="field"><span>Language</span><select name="language">${languages("en")}</select></label><label class="field"><span>Check frequency</span><select name="frequency"><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="biweekly">Every two weeks</option><option value="monthly">Monthly</option></select></label></div><details class="query-advanced"><summary>${kind === "ai" ? "Device (Google sources)" : "Search depth & device"}</summary><div class="field-row">${kind !== "ai" ? `<label class="field"><span>Store search depth</span><select name="depth"><option value="1">50 Apple results / 1 Play page</option><option value="2">100 Apple results / up to 2 Play pages</option><option value="3">150 Apple results / up to 3 Play pages</option></select></label>` : ""}<label class="field"><span>Device</span><select name="device"><option value="">Source default</option><option value="mobile">Mobile</option><option value="desktop">Desktop</option><option value="tablet">Tablet</option></select></label></div></details><div class="notice" id="query-estimate">Add queries to see the estimated monthly search usage.</div><div class="form-footer">${inWizard ? '<button class="button" type="button" data-action="wizard-back-to-app">Back to app</button><button class="button" type="button" data-action="skip-queries">Add queries later</button>' : ""}<button class="button primary" type="submit">${inWizard ? "Start tracking" : "Add queries & check"} ${icon("arrow")}</button></div></form>`;
}
function addQueries(kind = "all") {
  if (!state.apps.some((a) => !a.archived)) {
    startWizard();
    return;
  }
  wizard = null;
  showModal(
    kind === "store"
      ? "Follow a keyword"
      : kind === "ai"
        ? "Follow a question"
        : "Add tracking queries",
    "",
    queryForm(appId, kind),
  );
}
const monitorPlatform = (monitor) =>
  monitor.source === "apple_app_store"
    ? "ios"
    : monitor.source === "google_play"
      ? "android"
      : null;
function comparisonValue(data, store) {
  if (!data) return '<span class="tag neutral">Awaiting results</span>';
  if (store)
    return data.position
      ? `<span class="rank-number"><span>#</span>${esc(data.position)}</span>`
      : `<span class="tag ${data.found ? "good" : "neutral"}">${data.found ? "Featured" : "Not found"}</span>`;
  if (!data.answer_available)
    return '<span class="tag neutral">No AI answer</span>';
  const label = data.mentioned
    ? "Mentioned"
    : data.app_link
      ? "App linked"
      : data.possible
        ? "Possible match"
        : "Not mentioned";
  return `<span class="tag ${data.mentioned || data.app_link ? "good" : data.possible ? "warn" : "neutral"}">${label}</span><span class="tag ${data.cited ? "blue" : "neutral"}">${data.cited ? "Cited" : "Not cited"}</span>`;
}
async function trackedApps(id) {
  const request = ++comparisonRequest;
  wizard = null;
  showModal(
    "Apps & competitors",
    "Loading this search…",
    '<div class="skeleton"></div>',
  );
  modal.dataset.monitorId = String(id);
  const result = await api(`monitors/${id}/targets`);
  if (
    !modal.open ||
    modal.dataset.monitorId !== String(id) ||
    request !== comparisonRequest
  )
    return;
  const monitor = result.monitor,
    platform = monitorPlatform(monitor);
  result.targets.sort(
    (a, b) => Number(b.app_id === appId) - Number(a.app_id === appId),
  );
  const body = `<div class="comparison-search">${sourceBadge(monitor.source)}<div><h3>${esc(monitor.query)}</h3><p>${sourceLabels[monitor.source]} · ${esc(monitor.country === "global" ? "Global" : monitor.country.toUpperCase() + " · " + monitor.language.toUpperCase())}${monitor.enabled ? "" : " · Paused"}</p></div></div>
    <section aria-labelledby="tracked-apps-title"><div class="comparison-heading"><div><h3 id="tracked-apps-title">Tracked apps <span class="muted">${result.targets.length}</span></h3><p>${result.run_id ? `Last successful check · ${when(result.checked_at)}` : "Results will appear after this search completes a check."}</p></div>${result.run_id ? button("View saved check " + icon("arrow"), "run", "small", `data-id="${result.run_id}"`) : ""}</div>
    <div class="comparison-list">${result.targets
      .map((target) => {
        const app = appById(target.app_id);
        return `<div class="comparison-row"><div class="comparison-app">${appImage(app)}<div><strong>${esc(app?.name || "Tracked app")}</strong><small>${app?.archived ? "Archived" : "Tracking this search"}</small></div></div><div class="comparison-values">${comparisonValue(target.data, !!platform)}</div></div>`;
      })
      .join("")}</div></section>
    <section class="new-competitor"><div><strong>Compare across more searches</strong><p>Choose a competitor, store listings, queries and countries in Apps and competitors.</p></div>${button(icon("plus") + " Add a competitor", "add-competitor", "", `data-app-id="${originalForMonitor(monitor.id)}"`)}</section>`;
  showModal(
    "Apps & competitors",
    "Compare app visibility for this search.",
    body,
    button("Open query matrix", "matrix", "small", `data-id="${id}"`),
  );
  modal.dataset.monitorId = String(id);
}

function estimateForm() {
  const form = $("#query-form") || $("#country-form");
  if (!form) return;
  try {
    const monitors = formMonitors(form),
      usage = searchUsage(monitors, state.monitors),
      requested = new Set(monitors.map(monitorKey)),
      paused = state.monitors.filter(
        (m) => !m.enabled && requested.has(monitorKey(m)),
      ).length;
    $("#query-estimate").textContent = monitors.length
      ? `${usage.created} new tracked ${usage.created === 1 ? "search" : "searches"} · approximately ${num(usage.min)}${usage.min !== usage.max ? "–" + num(usage.max) : ""} additional searches/month.${paused ? ` ${paused} existing ${paused === 1 ? "search is" : "searches are"} paused and will stay paused. Resume from Store rankings or AI visibility.` : ""}`
      : "Add queries to see the estimated monthly search usage.";
  } catch (error) {
    $("#query-estimate").textContent = error.message;
  }
}
async function showRun(id) {
  showModal(
    "Search evidence",
    "Loading this saved check…",
    '<div class="skeleton"></div>',
  );
  try {
    const r = await api("runs/" + id),
      result = r.result;
    if (r.kind === "listing_history" && r.listing_snapshot_id) {
      await insights.showSnapshot(r.listing_snapshot_id);
      return;
    }
    let body = `<div class="run-meta">${statusTag(r.status)}<span class="tag neutral">${r.kind === "listing_history" ? "Listing history" : sourceLabels[r.params.source] || "Listing refresh"}</span><span class="tag neutral">${esc(countryName(r.params.country || "global"))}</span><span class="tag neutral">${when(r.started_at || r.created_at)}</span><span class="tag neutral">${r.requests_count} requests</span></div>${r.error ? `<div class="notice error">${esc(r.error)}</div>` : ""}${r.observations.map((o) => `<h3>${esc(appById(o.app_id)?.name || "Tracked app")}${o.retrospective ? " · Reanalyzed" : ""}</h3>${o.data.evidence?.map((e) => `<div class="evidence"><span class="tag good">${esc(e.type.replaceAll("_", " "))}</span> ${esc(e.text)}${e.url ? `<br><a class="text-link" href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">Open matching source ${icon("external")}</a>` : ""}</div>`).join("") || `<p class="panel-subtitle">No matching evidence in this check.${result.kind === "ai" ? " If the answer uses another name, add it as an alias in My apps → Manage, then reanalyze history." : ""}</p>`}`).join("")}`;
    if (result.kind === "ai")
      body += `<div class="separator"></div><h3>Saved answer</h3><div class="ai-answer">${esc(result.text || "No answer returned.")}</div><div class="references">${result.references.map((ref) => `<a class="reference" href="${esc(ref.link)}" target="_blank" rel="noopener noreferrer">${esc(ref.title)} ${icon("external")}<small>${esc(ref.link)}</small></a>`).join("")}</div>`;
    if (result.kind === "store")
      body += `<div class="separator"></div><h3>Store results</h3><p class="panel-subtitle">${result.results_checked} results checked.</p><div class="table-wrap"><table><thead><tr><th>POSITION</th><th>APP</th><th>IDENTIFIER</th><th>SECTION</th></tr></thead><tbody>${result.items.map((item) => `<tr><td>${item.primary && item.position ? "#" + esc(item.position) : "Unranked"}</td><td class="query-cell"><a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)} ${icon("external")}</a></td><td>${esc(item.external_id)}</td><td>${esc(item.section)}</td></tr>`).join("")}</tbody></table></div>`;
    showModal(
      esc(r.params.query || r.params.external_id || "Search evidence"),
      "Results and sources saved for this search.",
      body,
    );
  } catch (e) {
    $("#modal-error").textContent = e.message;
  }
}
let profileChart;
modal.addEventListener("close", () => {
  placeLoadingIndicator();
  profileChart?.destroy();
  profileChart = null;
});
async function showProfiles(id) {
  const app = appById(id);
  const data = await api(`apps/${id}/profiles`);
  const profiles = data.profiles;
  showModal(
    esc(app.name) + " · listing statistics",
    "Ratings and review counts as reported by each store.",
    `<div class="chart-area"><canvas id="profile-chart" role="img" aria-label="App rating history"></canvas></div><div class="table-wrap"><table><thead><tr><th>CHECKED</th><th>STORE</th><th>RATING</th><th>RATINGS / REVIEWS</th><th>DOWNLOADS</th></tr></thead><tbody>${[
      ...profiles,
    ]
      .reverse()
      .map(
        (p) =>
          `<tr><td>${when(p.checked_at)}</td><td>${p.platform === "ios" ? "App Store" : "Google Play"}</td><td>${esc(p.data.rating ?? "—")}</td><td>${esc(p.data.reviews ?? "—")}</td><td>${esc(p.data.downloads ?? "—")}</td></tr>`,
      )
      .join("")}</tbody></table></div>`,
    `<small>${profiles.length} saved listing snapshots</small>`,
  );
  if (window.Chart) {
    const timestamps = [...new Set(profiles.map((p) => p.checked_at))].sort(
      (a, b) => a - b,
    );
    profileChart = new Chart($("#profile-chart"), {
      type: "line",
      data: {
        labels: timestamps.map(when),
        datasets: ["ios", "android"].map((platform) => ({
          label: platform === "ios" ? "App Store" : "Google Play",
          data: timestamps.map(
            (t) =>
              profiles.find(
                (p) => p.checked_at === t && p.platform === platform,
              )?.data.rating ?? null,
          ),
          borderColor: sourceColors[platformSource(platform)],
          backgroundColor: sourceColors[platformSource(platform)],
          pointRadius: 4,
          spanGaps: true,
        })),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        scales: { y: { min: 0, max: 5 } },
        plugins: {
          legend: {
            labels: {
              color: "#313131",
              font: { size: 12, weight: 500 },
              usePointStyle: true,
              pointStyle: "circle",
              boxWidth: 10,
              boxHeight: 10,
              padding: 16,
            },
          },
        },
      },
    });
  } else
    $("#profile-chart").parentElement.innerHTML =
      '<div class="chart-fallback">Rating snapshots are available in the table below.</div>';
}
function manageCompetitorLinks(app) {
  const parents = competitorParents(app, true);
  return `<h3>Competitor for</h3><p>Removing a link keeps this app, its shared queries, and saved history.</p>${
    parents.length
      ? `<ul class="competitor-link-list">${parents.map((parent) => `<li><span><strong>${esc(parent.name)}</strong>${parent.archived ? '<span class="tag neutral">Archived</span>' : ""}</span>${button("Remove link", "remove-competitor-link", "small", `type="button" data-app-id="${app.id}" data-parent-id="${parent.id}" aria-label="Remove competitor link to ${esc(parent.name)}"`)}</li>`).join("")}</ul>`
      : '<p class="muted" role="status">No competitor links.</p>'
  }`;
}
function editApp(id) {
  const app = appById(id);
  showModal(
    "Manage " + esc(app.name),
    "Edit matching names or archive this app while keeping its history.",
    `<section class="manage-listing-refresh"><div><h3>Store details</h3><p>Update ratings, review counts, and listing details. Uses ${app.listings.length} search ${app.listings.length === 1 ? "credit" : "credits"}.</p></div>${button(icon("refresh") + " Refresh store details", "refresh-profiles", "", `type="button" data-id="${id}"`)}</section><section class="manage-competitor-links" id="manage-competitor-links" data-app-id="${id}" tabindex="-1" ${competitorParents(app, true).length ? "" : "hidden"}>${manageCompetitorLinks(app)}</section><form id="edit-app-form" data-id="${id}"><label class="field"><span>App name</span><input class="input" name="name" value="${esc(app.name)}" required maxlength="160"></label><label class="field"><span>Aliases · comma-separated</span><input class="input" name="aliases" value="${esc(app.aliases.join(", "))}"></label><label class="field"><span>App website (optional)</span><input class="input" name="website" value="${esc(app.website)}" placeholder="https://yourapp.com" aria-describedby="edit-website-help"><small id="edit-website-help">Used to track AI citations linking to your app’s website.</small></label><label class="check-chip"><input type="checkbox" name="archived" ${app.archived ? "checked" : ""}>Archive app and pause its automatic checks</label><p class="panel-subtitle">AI matching uses the app name, aliases, and full store titles. Add the short product name as an alias if needed. Reanalyze history after saving changes to update earlier matches.</p><div class="form-footer">${button("Reanalyze history", "reanalyze", "", `data-id="${id}"`)}<button class="button primary" type="submit">Save changes</button></div></form>`,
  );
}
async function saveQueries(form) {
  const monitors = formMonitors(form);
  if (!monitors.length)
    throw new Error("Add at least one query and select a matching source.");
  const result = await api("monitors/batch", {
    method: "POST",
    body: { monitors },
  });
  await sync();
  if (form.dataset.wizard === "true") {
    wizard.step = 4;
    renderWizard();
  } else modal.close();
  const paused = result.monitors.filter((monitor) => !monitor.enabled).length;
  toast(
    (result.created
      ? `${result.created} new tracking searches saved. Initial checks are queued.`
      : "Existing searches reused with their saved schedules.") +
      (paused
        ? ` ${paused} ${paused === 1 ? "search remains" : "searches remain"} paused. Resume from Store rankings or AI visibility.`
        : ""),
  );
}
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form.id) return;
  event.preventDefault();
  if (isLoading(form)) return;
  const submit = event.submitter || $('button[type="submit"]', form);
  if (submit?.disabled) return;
  const finishLoading = beginLoading(submit, form);
  const errorBox = $("#modal-error");
  if (errorBox) errorBox.textContent = "";
  try {
    if (await competitors.submit(form)) return;
    if (await insights.submit(form)) return;
    if (form.id === "query-group-form") {
      const data = new FormData(form);
      await api("monitor-groups", {
        method: "PATCH",
        body: {
          monitor_ids: form.dataset.ids.split(",").map(Number),
          frequency: data.get("frequency") || null,
          enabled:
            data.get("enabled") === "" ? null : data.get("enabled") === "true",
        },
      });
      modal.close();
      await sync();
      toast("Query settings saved for all countries and sources.");
      return;
    }
    if (["wizard-key-form", "settings-key-form"].includes(form.id)) {
      await api("key", {
        method: "POST",
        body: { api_key: new FormData(form).get("api_key") },
      });
      form.reset();
      await sync();
      if (form.id === "wizard-key-form") {
        wizard.step = 2;
        renderWizard();
      } else toast("SerpApi connected.");
    } else if (form.id === "discovery-form") {
      rememberWizard();
      const currentWizard = wizard,
        currentPlatform = wizard.platform;
      submit.textContent = "Searching…";
      const result = await api("discover", {
        method: "POST",
        body: {
          platform: wizard.platform,
          query: wizard.query,
          country: wizard.country,
          language: wizard.language,
        },
      });
      if (
        wizard !== currentWizard ||
        wizard.platform !== currentPlatform ||
        !modal.open
      )
        return;
      candidates = result.candidates;
      renderWizard();
      if (!candidates.length)
        $("#modal-error").textContent =
          "No apps found in this market. Try a store URL or another name.";
    } else if (form.id === "add-targets-form") {
      const ids = new FormData(form).getAll("app_ids").map(Number);
      if (!ids.length) throw new Error("Choose at least one app to add.");
      const result = await api(`monitors/${form.dataset.id}/targets`, {
        method: "POST",
        body: { app_ids: ids },
      });
      await sync();
      if (form.isConnected && modal.open)
        await trackedApps(Number(form.dataset.id));
      toast(`${result.added_app_ids.length} app(s) added.`);
    } else if (["query-form", "country-form"].includes(form.id))
      await saveQueries(form);
    else if (form.id === "password-form") {
      const data = new FormData(form);
      if (data.get("new_password") !== data.get("confirm_password"))
        throw new Error("Passwords do not match.");
      const result = await api("auth/password", {
        method: "POST",
        body: {
          current_password: data.get("current_password"),
          new_password: data.get("new_password"),
        },
      });
      csrfToken = result.csrf_token;
      form.reset();
      toast("Password changed. Other sessions have been signed out.");
    } else if (form.id === "edit-app-form") {
      const data = new FormData(form);
      await api("apps/" + form.dataset.id, {
        method: "PATCH",
        body: {
          name: data.get("name"),
          website: data.get("website"),
          aliases: data
            .get("aliases")
            .split(",")
            .map((x) => x.trim())
            .filter(Boolean),
          archived: data.has("archived"),
        },
      });
      modal.close();
      await sync();
      toast("App updated.");
    } else if (form.id === "replace-query-form") {
      const query = new FormData(form).get("query").trim();
      await api(`monitors/${form.dataset.id}/replace`, {
        method: "POST",
        body: { query },
      });
      modal.close();
      await sync();
      toast("New query started. Previous query history was preserved.");
    }
  } catch (e) {
    if ($("#modal-error") && modal.open)
      $("#modal-error").textContent = e.message;
    else toast(e.message, true);
  } finally {
    finishLoading();
    if (submit && submit.isConnected) {
      if (form.id === "discovery-form")
        submit.innerHTML = icon("search") + " Search";
    }
  }
});
document.addEventListener("click", async (event) => {
  const el = event.target.closest("[data-action]");
  if (!el || el.disabled || isLoading(el)) return;
  const action = el.dataset.action;
  const finishLoading = beginLoading(el);
  try {
    if (competitors.click(el)) return;
    if (await insights.click(el)) return;
    if (
      ["expand-query", "manage-query-group", "check-query-group"].includes(
        action,
      )
    ) {
      const group = pageQueryGroups().find(
        (group) => group.id === Number(el.dataset.id),
      );
      if (!group) return;
      if (action === "expand-query") {
        if (expandedQueries.has(group.key)) expandedQueries.delete(group.key);
        else expandedQueries.add(group.key);
        render();
        $(`[data-action="expand-query"][data-id="${group.id}"]`).focus();
      } else if (action === "manage-query-group") manageQueryGroup(group);
      else {
        el.disabled = true;
        const result = await api("monitor-groups/check", {
          method: "POST",
          body: { monitor_ids: group.monitors.map((monitor) => monitor.id) },
        });
        await sync();
        toast(
          `${result.run_ids.length} checks queued.${state.sync_paused ? " Resume workspace sync to run them." : ""}`,
        );
      }
      return;
    }
    if (action === "toggle-sync") {
      el.disabled = true;
      const paused = !trackingState.sync_paused;
      await api("sync", { method: "PATCH", body: { paused } });
      await sync();
      if (modal.open && modal.dataset.view === "search-summary")
        showSearchSummary();
      toast(
        paused
          ? "Workspace sync paused. Queued checks are on hold."
          : "Workspace sync resumed. Individual pause settings are preserved.",
      );
      return;
    }
    if (action === "sync-settings") {
      event.preventDefault();
      modal.close();
      location.hash = "settings";
      await sync();
      $("#sync-settings")?.focus();
      return;
    }
    if (action === "dismiss-listing-intro") {
      el.disabled = true;
      await api("hints/listing-history/dismiss", { method: "POST" });
      await sync();
      return;
    }
    if (action === "history-view") {
      chartView = el.dataset.view;
      if (
        chartView === "distribution" &&
        sourceFilter &&
        !["apple_app_store", "google_play"].includes(sourceFilter)
      )
        sourceFilter = "";
      await sync();
      return;
    }
    if (
      [
        "skip-checklist-step",
        "dismiss-checklist",
        "restore-checklist",
      ].includes(action)
    ) {
      el.disabled = true;
      await api("onboarding", {
        method: "POST",
        body: {
          action:
            action === "skip-checklist-step"
              ? "skip"
              : action === "dismiss-checklist"
                ? "dismiss"
                : "restore",
          ...(el.dataset.step ? { step: el.dataset.step } : {}),
        },
      });
      if (action === "restore-checklist") location.hash = "overview";
      await sync();
      if (action === "restore-checklist" && !state.onboarding.visible)
        toast("All setup steps are complete.");
    }
    if (action === "logout") {
      await api("auth/logout", { method: "POST" });
      leaving = true;
      document.body.replaceChildren();
      location.replace("/login");
      return;
    }
    if (action === "menu")
      setNavigation(!$("#sidebar").classList.contains("open"));
    if (action === "close-menu") setNavigation(false, true);
    if (action === "close-modal") modal.close();
    if (action === "search-summary") showSearchSummary();
    if (action === "dismiss-progress") {
      progressDismissed = true;
      renderSearchProgress();
    }
    if (action === "summary-route") {
      event.preventDefault();
      modal.close();
      appId = 0;
      sourceFilter = countryFilter = "";
      if (el.dataset.route === "activity") {
        days = 30;
        customStart = customEnd = "";
      }
      location.hash = el.dataset.route;
      await sync();
    }
    if (["setup", "add-app"].includes(action)) startWizard();
    if (action === "link-store") startWizard(appById(Number(el.dataset.id)));
    if (action === "tracked-apps") await trackedApps(Number(el.dataset.id));
    if (action === "wizard-platform") {
      rememberWizard();
      wizard.platform = el.dataset.platform;
      candidates = [];
      renderWizard();
    }
    if (action === "select-candidate") {
      rememberWizard();
      const candidate = candidates[Number(el.dataset.index)];
      selectedCandidates[wizard.platform] = candidate;
      if (!wizard.name) wizard.name = candidate.title.split(":")[0];
      renderWizard();
    }
    if (action === "save-wizard-app") {
      rememberWizard();
      const currentWizard = wizard;
      const picked = Object.values(selectedCandidates);
      if (!picked.length)
        throw new Error("Select at least one app from the search results.");
      if (!wizard.name.trim()) throw new Error("Enter a name for this app.");
      el.disabled = true;
      el.textContent = "Connecting store listings…";
      const result = wizard.savedAppId
        ? { id: wizard.savedAppId }
        : await api(wizard.appId ? `apps/${wizard.appId}/listings` : "apps", {
            method: "POST",
            body: {
              name: wizard.name,
              candidate_tokens: picked.map((c) => c.token),
              aliases: wizard.aliases
                .split(",")
                .map((x) => x.trim())
                .filter(Boolean),
              website: wizard.website,
            },
          });
      if (wizard !== currentWizard || !modal.open) {
        await sync();
        return;
      }
      wizard.appId = result.id;
      if (wizard.monitorId) {
        wizard.savedAppId = result.id;
        const monitorId = wizard.monitorId;
        await api(`monitors/${monitorId}/targets`, {
          method: "POST",
          body: { app_ids: [result.id] },
        });
        await sync();
        if (wizard === currentWizard && modal.open)
          await trackedApps(monitorId);
        toast("Competitor added.");
        return;
      }
      appId = result.id;
      localStorage.setItem("apptrail-app", appId);
      await sync();
      if (wizard.linkOnly) {
        modal.close();
        toast("Store listing connected.");
      } else {
        candidates = [];
        selectedCandidates = {};
        const connected = appById(wizard.appId).listings;
        wizard.step = connected.length === 2 ? 3 : 2;
        if (connected.length === 1) {
          wizard.platform = connected[0].platform === "ios" ? "android" : "ios";
          wizard.query = wizard.name;
        }
        renderWizard();
      }
    }
    if (action === "wizard-queries") {
      wizard.step = 3;
      renderWizard();
    }
    if (action === "wizard-back-to-app") {
      const form = $("#query-form");
      wizard.queryDraft = {
        values: new FormData(form),
        sources: $$('input[name="sources"]', form).map((input) => input.value),
      };
      candidates = [];
      selectedCandidates = {};
      wizard.step = 2;
      renderWizard();
    }
    if (action === "skip-queries") {
      wizard.step = 4;
      renderWizard();
    }
    if (action === "finish-wizard") {
      modal.close();
      location.hash = "overview";
      await sync();
    }
    if (action === "add-queries")
      addQueries(
        route() === "ai" ? "ai" : route() === "rankings" ? "store" : "all",
      );
    if (action === "add-countries") addCountries(Number(el.dataset.id));
    if (action === "selected-countries") {
      el.setAttribute(
        "aria-pressed",
        String(el.getAttribute("aria-pressed") !== "true"),
      );
      el.title =
        el.getAttribute("aria-pressed") === "true"
          ? "Show all countries"
          : "Show selected countries";
      updateCountryPicker();
    }
    if (["select-countries", "clear-countries"].includes(action)) {
      const form = el.closest("form");
      if (action === "clear-countries")
        $(".country-count", form).setAttribute("aria-pressed", "false");
      $$(".country-option", form).forEach((row) => {
        const input = $("input", row);
        if (action === "clear-countries") input.checked = false;
        else if (!row.hidden && !input.disabled) input.checked = true;
      });
      updateCountryPicker();
      estimateForm();
    }
    if (action === "add-ai") addQueries("ai");
    if (action === "add-store") addQueries("store");
    if (action === "suggest-question") {
      const input = $('textarea[name="ai_queries"]');
      input.value += (input.value.trim() ? "\n" : "") + el.dataset.text;
      estimateForm();
    }
    if (action === "range") {
      days = Number(el.dataset.days);
      if (!days) {
        customEnd = localDateValue();
        customStart = localDateValue(new Date(Date.now() - 30 * 86400000));
      }
      await sync();
    }
    if (action === "chart-mode") {
      chartMode = el.dataset.mode;
      comparisonCountries = null;
      render();
    }
    if (["chart-countries-all", "chart-countries-clear"].includes(action)) {
      comparisonCountries = action === "chart-countries-all" ? null : [];
      render();
      $(".chart-country-picker").open = true;
    }
    if (action === "select-app") {
      appId = Number(el.dataset.id);
      localStorage.setItem("apptrail-app", appId);
      location.hash = "overview";
      await sync();
    }
    if (action === "edit-app") editApp(Number(el.dataset.id));
    if (action === "remove-competitor-link") {
      el.disabled = true;
      const id = Number(el.dataset.appId);
      await api(`apps/${el.dataset.parentId}/competitors/${id}`, {
        method: "DELETE",
      });
      await sync();
      const section = $("#manage-competitor-links");
      if (modal.open && Number(section?.dataset.appId) === id) {
        const restoreFocus = section.contains(document.activeElement);
        section.innerHTML = manageCompetitorLinks(appById(id));
        if (restoreFocus) section.focus();
      }
      toast("Competitor link removed. Shared tracking and history are kept.");
      return;
    }
    if (action === "restore-app") {
      const app = appById(Number(el.dataset.id));
      el.disabled = true;
      await api(`apps/${app.id}`, {
        method: "PATCH",
        body: {
          name: app.name,
          aliases: app.aliases,
          website: app.website,
          archived: false,
        },
      });
      await sync();
      toast(`${app.name} restored.`);
    }
    if (action === "profiles") await showProfiles(Number(el.dataset.id));
    if (action === "refresh-profiles") {
      el.disabled = true;
      await api(`apps/${el.dataset.id}/refresh`, { method: "POST" });
      await sync();
      toast("Store detail refresh queued.");
    }
    if (action === "edit-query") {
      const m = state.monitors.find((m) => m.id === Number(el.dataset.id));
      showModal(
        "Replace tracking query",
        "Start a new series and pause the old one. Existing history stays available.",
        `<form id="replace-query-form" data-id="${m.id}"><label class="field"><span>Search query</span><input class="input" name="query" value="${esc(m.query)}" maxlength="500" required></label><div class="notice">This query follows ${m.app_ids.filter((id) => !appById(id)?.archived).length} active app(s). They will follow the replacement.</div><button class="button primary" type="submit">Replace & check</button></form>`,
      );
    }
    if (action === "reanalyze") {
      el.disabled = true;
      const result = await api(`apps/${el.dataset.id}/reanalyze`, {
        method: "POST",
      });
      toast(
        `${result.updated} saved checks reanalyzed. Save name changes first if needed.`,
      );
      await sync();
    }
    if (action === "run") await showRun(el.dataset.id);
    if (action === "check-all") {
      el.disabled = true;
      const result = await api("check?" + scopeParams(), {
        method: "POST",
      });
      await sync();
      toast(`${result.run_ids.length} checks queued.`);
    }
    if (action === "check-one") {
      el.disabled = true;
      await api(`monitors/${el.dataset.id}/check`, { method: "POST" });
      await sync();
      toast("Check queued.");
    }
    if (action === "toggle-monitor") {
      const m = state.monitors.find((m) => m.id === Number(el.dataset.id));
      await api("monitors/" + m.id, {
        method: "PATCH",
        body: { enabled: !m.enabled },
      });
      await sync();
      toast(m.enabled ? "Automatic checks paused." : "Tracking resumed.");
    }
    if (action === "refresh-account") {
      el.disabled = true;
      await api("account/refresh", { method: "POST" });
      await sync();
      toast("Account credits refreshed.");
    }
  } catch (e) {
    if (modal.open && $("#modal-error"))
      $("#modal-error").textContent = e.message;
    else toast(e.message, true);
  } finally {
    finishLoading();
    if (el.isConnected) {
      if (action === "save-wizard-app") el.textContent = wizardSaveLabel();
    }
  }
});
document.addEventListener("change", async (event) => {
  const el = event.target;
  const finishLoading = beginLoading();
  try {
    competitors.change(el);
    if (await insights.change(el)) return;
    if (el.id === "chart-group") {
      chartGroup = el.value;
      if (chartGroup === "country" && countryFilter) {
        countryFilter = "";
        await sync();
      } else render();
    }
    if (el.id === "comparison-source" || el.id === "comparison-query") {
      if (el.id === "comparison-source") {
        comparisonSource = el.value;
        comparisonQuery = "";
      } else comparisonQuery = el.value;
      comparisonCountries = null;
      render();
    }
    if (el.dataset.chartCountry) {
      comparisonCountries = $$("[data-chart-country]:checked").map(
        (input) => input.dataset.chartCountry,
      );
      render();
      $(".chart-country-picker").open = true;
      $(`[data-chart-country="${el.dataset.chartCountry}"]`).focus();
    }
    if (el.closest("#add-targets-form")) {
      const form = el.closest("form"),
        count = $$('input[name="app_ids"]:checked', form).length,
        submit = $('button[type="submit"]', form);
      submit.disabled = count === 0;
      submit.textContent = count
        ? `Add ${count} ${count === 1 ? "app" : "apps"}`
        : "Add selected apps";
    }
    if (el.id === "app-filter") {
      appId = Number(el.value);
      comparisonCountries = null;
      localStorage.setItem("apptrail-app", appId);
      await sync();
    }
    if (el.id === "source-filter") {
      sourceFilter = el.value;
      if (
        sourceFilter &&
        !["apple_app_store", "google_play"].includes(sourceFilter)
      ) {
        chartView = "trend";
        chartMode = "ai";
      }
      comparisonCountries = null;
      await sync();
    }
    if (el.id === "country-filter") {
      countryFilter = el.value;
      if (countryFilter) chartGroup = "source";
      await sync();
    }
    if (el.id === "date-start" || el.id === "date-end") {
      customStart = $("#date-start").value;
      customEnd = $("#date-end").value;
      await sync();
    }
    if (el.dataset.frequency) {
      await api("monitors/" + el.dataset.frequency, {
        method: "PATCH",
        body: { frequency: el.value },
      });
      await sync();
      toast("Query schedule updated.");
    }
    if (el.id === "query-app") {
      const previous = $("#query-form"),
        inputs = new FormData(previous),
        previousSources = new Set(
          $$('input[name="sources"]', previous).map((input) => input.value),
        ),
        kind = previous.dataset.kind;
      $(".modal-body").innerHTML =
        queryForm(Number(el.value), kind) +
        '<div id="modal-error" class="error-message" role="alert"></div>';
      const next = $("#query-form");
      for (const name of [
        "store_queries",
        "ai_queries",
        "language",
        "frequency",
        "device",
        "depth",
      ]) {
        const input = $(`[name="${name}"]`, next);
        if (input && inputs.has(name)) input.value = inputs.get(name);
      }
      for (const name of ["sources", "countries"]) {
        const values = inputs.getAll(name);
        $$(`input[name="${name}"]`, next).forEach((input) => {
          if (name !== "sources" || previousSources.has(input.value))
            input.checked = values.includes(input.value);
        });
      }
    }
    if (el.closest("#query-form, #country-form") || el.id === "query-app") {
      updateCountryPicker();
      estimateForm();
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    finishLoading();
  }
});
document.addEventListener("input", (event) => {
  competitors.input(event.target);
  if (event.target.matches(".competitor-filter")) {
    const form = event.target.closest("form"),
      query = event.target.value.trim().toLowerCase();
    $$(".competitor-option", form).forEach(
      (row) => (row.hidden = !row.dataset.appName.includes(query)),
    );
    $(".competitor-no-match", form).hidden = $$(
      ".competitor-option",
      form,
    ).some((row) => !row.hidden);
  }
  if (event.target.id === "country-search") updateCountryPicker();
  if (event.target.closest("#query-form, #country-form")) estimateForm();
});
document.addEventListener(
  "error",
  (event) => {
    if (event.target.tagName === "IMG") {
      event.target.src = "/static/favicon.svg";
    }
  },
  true,
);
const controlHint = document.createElement("div");
controlHint.id = "control-hint";
controlHint.className = "control-hint";
controlHint.role = "tooltip";
controlHint.hidden = true;
document.body.append(controlHint);
let hintTarget;
function hideControlHint() {
  hintTarget?.removeAttribute("aria-describedby");
  hintTarget = null;
  controlHint.hidden = true;
}
function showControlHint(target) {
  if (!target) return;
  hideControlHint();
  hintTarget = target;
  controlHint.textContent = target.dataset.tooltip;
  controlHint.hidden = false;
  target.setAttribute("aria-describedby", controlHint.id);
  const rect = target.getBoundingClientRect(),
    hint = controlHint.getBoundingClientRect();
  controlHint.style.left = `${Math.max(8, Math.min(innerWidth - hint.width - 8, rect.left + (rect.width - hint.width) / 2))}px`;
  controlHint.style.top = `${rect.top >= hint.height + 12 ? rect.top - hint.height - 8 : rect.bottom + 8}px`;
}
document.addEventListener("pointerover", (event) => {
  const target = event.target.closest("[data-tooltip]");
  if (target && target !== hintTarget) showControlHint(target);
});
document.addEventListener("pointerout", (event) => {
  if (hintTarget && !hintTarget.contains(event.relatedTarget))
    hideControlHint();
});
document.addEventListener("focusin", (event) =>
  showControlHint(event.target.closest("[data-tooltip]")),
);
document.addEventListener("focusout", hideControlHint);
document.addEventListener("pointerdown", hideControlHint);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") hideControlHint();
});
window.addEventListener("scroll", hideControlHint, true);
window.addEventListener("resize", hideControlHint);
window.addEventListener("hashchange", () => {
  setNavigation(false);
  sync();
});
$("#sidebar").addEventListener("click", (event) => {
  if (event.target.closest('a[href^="#"]')) setNavigation(false, true);
});
$$("[data-icon]").forEach((el) => {
  el.innerHTML =
    el.closest("nav") && sidebarPaths[el.dataset.icon]
      ? sidebarIcon(el.dataset.icon)
      : icon(el.dataset.icon);
});
const finishInitialLoading = beginLoading();
try {
  const identity = await api("auth/status");
  if (!identity.authenticated) {
    leaving = true;
    location.replace("/login");
  } else {
    csrfToken = identity.csrf_token;
    accountName = identity.username;
    regions = await api("regions");
    await sync();
    setInterval(() => {
      if (!leaving && !document.hidden) sync(true);
    }, 5000);
  }
} catch (e) {
  if (!leaving) toast(e.message, true);
} finally {
  finishInitialLoading();
}
window.addEventListener("pageshow", (event) => {
  if (event.persisted) location.reload();
});
