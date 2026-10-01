import { matrixRows, textDiff, mediaChanges, sameMedia } from "./insights-data.js";

const bucketLabels = {
  top3: "Top 3",
  top10: "4–10",
  top50: "11–50",
  beyond50: "51+",
  not_found: "Not found",
};
const fieldLabels = {
  title: "Title",
  subtitle: "Subtitle",
  description: "Description",
  version: "Version",
  release_notes: "Release notes",
  price: "Price",
  developer: "Developer",
  icon: "App icon",
  screenshots: "Screenshots",
  iphone_screenshots: "iPhone screenshots",
  ipad_screenshots: "iPad screenshots",
  watch_screenshots: "Watch screenshots",
};
const frequencies = {
  daily: "Daily",
  weekly: "Weekly",
  biweekly: "Every two weeks",
  monthly: "Monthly",
};
const intervals = { daily: 1, weekly: 7, biweekly: 14, monthly: 30 };

export function createInsightsUI(ctx) {
  const {
    api,
    esc,
    icon,
    button,
    when,
    countryName,
    showModal,
    modal,
    sourceLabels,
  } = ctx;
  let distribution,
    watches = [],
    selectedWatch = 0,
    timeline = { snapshots: [] },
    unchanged = false,
    matrix,
    matrixSource,
    matrixMonitor,
    requestVersion = 0;
  const activeWatches = () =>
    watches.filter(
      (w) =>
        (!ctx.appId() || w.app_id === ctx.appId()) &&
        (!ctx.countryFilter() || w.country === ctx.countryFilter()),
    );
  const labelField = (field) =>
    fieldLabels[field] || field.replaceAll("_", " ");
  const rank = (value) =>
    Number.isFinite(Number(value)) && Number(value) > 0
      ? `#${Number(value)}`
      : "Not found";

  async function load(route, params, distributionVisible) {
    const result = {};
    if (distributionVisible && route === "overview")
      result.distribution = await api("ranking-distribution?" + params);
    if (route === "listing-history") {
      result.watches = (await api("listing-watches")).watches;
      const available = result.watches.filter(
        (w) =>
          (!ctx.appId() || w.app_id === ctx.appId()) &&
          (!ctx.countryFilter() || w.country === ctx.countryFilter()),
      );
      result.selectedWatch = available.some((w) => w.id === selectedWatch)
        ? selectedWatch
        : available[0]?.id || 0;
      result.timeline = result.selectedWatch
        ? await api(
            `listing-watches/${result.selectedWatch}/history?include_unchanged=${unchanged}`,
          )
        : { snapshots: [] };
    }
    return result;
  }
  function apply(result) {
    if (result.distribution) distribution = result.distribution;
    if (result.watches) {
      watches = result.watches;
      selectedWatch = result.selectedWatch;
      timeline = result.timeline;
    }
  }

  function distributionPanel() {
    if (!distribution)
      return '<div class="insight-empty">Loading ranking distribution…</div>';
    const d = distribution;
    return `<div class="distribution"><div class="distribution-total"><strong>${d.total}</strong><span>ranked or searched combinations<span class="muted">Latest check per app, keyword and country</span></span></div>
      <div class="distribution-bar" aria-label="Ranking distribution">${Object.keys(
        bucketLabels,
      )
        .map((key) =>
          d.counts[key]
            ? `<button class="bucket-${key}" data-action="distribution-bucket" data-bucket="${key}" data-flex="${d.counts[key]}" aria-label="${bucketLabels[key]}: ${d.counts[key]} combinations">${Math.round((d.counts[key] / d.total) * 100) >= 8 ? d.counts[key] : ""}</button>`
            : "",
        )
        .join("")}</div>
      <div class="distribution-key">${Object.entries(bucketLabels)
        .map(
          ([key, label]) =>
            `<button data-action="distribution-bucket" data-bucket="${key}"><i class="bucket-${key}"></i><span>${label}</span><strong>${d.counts[key]}</strong><span class="bucket-percentage">${d.total ? Math.round((d.counts[key] / d.total) * 100) : 0}%</span><small class="${d.deltas[key] > 0 ? "up" : d.deltas[key] < 0 ? "down" : ""}">${d.paired ? (d.deltas[key] > 0 ? "+" : "") + d.deltas[key] : "—"}</small></button>`,
        )
        .join("")}</div>
      <p class="insight-note">Changes compare ${d.paired} combinations checked successfully in both this period and the preceding equal-length period. ${d.failed} failed checks and ${d.featured} featured-only placements are excluded. “Not found” means outside the results checked.</p>
      ${d.movements.length ? `<details class="movement-details"><summary>Biggest changes <span>${d.movements.length}</span></summary><div>${d.movements.map((r) => `<button class="movement-row" data-action="run" data-id="${r.run_id}"><span><strong>${esc(r.params.query)}</strong><small>${esc(ctx.appById(r.app_id)?.name)} · ${esc(countryName(r.params.country))} · ${sourceLabels[r.params.source]}</small></span><span>${rank(r.before)} <span aria-hidden="true">→</span> <strong>${rank(r.after)}</strong></span></button>`).join("")}</div></details>` : ""}</div>`;
  }

  async function showMatrix(id) {
    const request = ++requestVersion;
    matrixMonitor = id;
    showModal(
      "Query matrix",
      "Loading saved comparisons…",
      '<div class="skeleton"></div>',
    );
    modal.dataset.view = "matrix";
    const data = await api(`monitors/${id}/matrix`);
    if (
      !modal.open ||
      modal.dataset.view !== "matrix" ||
      request !== requestVersion
    )
      return;
    matrix = data;
    matrixSource = data.source;
    renderMatrix();
  }
  function renderMatrix() {
    const sources = [...new Set(matrix.monitors.map((m) => m.source))];
    const store = ["apple_app_store", "google_play"].includes(matrixSource);
    const rows = matrixRows(matrix, matrixSource);
    function cell(value) {
      const r = value.latest;
      if (!value.tracked)
        return '<td><span class="matrix-missing">Not tracked</span></td>';
      if (!r) return '<td><span class="matrix-missing">Not checked</span></td>';
      const data = r.data;
      let label = "Check failed",
        shade = "failed",
        detail = "";
      if (r.status === "success") {
        if (store) {
          label = data.position
            ? `#${data.position}`
            : data.found
              ? "Featured"
              : "Not found";
          shade =
            data.position <= 3 && data.position
              ? "strong"
              : data.position <= 10 && data.position
                ? "good"
                : data.position
                  ? "ranked"
                  : "absent";
          detail =
            value.delta === null
              ? `${data.results_checked ?? "—"} results checked`
              : value.delta === 0
                ? "No change"
                : `${value.delta > 0 ? "↑" : "↓"} ${Math.abs(value.delta)} since prior check`;
        } else {
          label =
            data.answer_available === false
              ? "No answer"
              : data.mentioned
                ? "Mentioned"
                : data.app_link
                  ? "App linked"
                  : data.possible
                    ? "Possible match"
                    : "Not mentioned";
          shade = data.mentioned ? "strong" : data.cited ? "good" : "absent";
          detail = data.cited ? "Cited" : "Not cited";
        }
      }
      return `<td><button class="matrix-cell ${shade}" data-action="run" data-id="${r.run_id}" aria-label="${esc(value.app.name)}: ${esc(label)}, ${esc(countryName(r.params.country))}, checked ${when(r.checked_at)}"><strong>${esc(label)}</strong><span>${esc(detail)}</span><small>${when(r.checked_at)}</small></button></td>`;
    }
    showModal(
      esc(matrix.query),
      "Compare apps across the countries you track.",
      `<div class="matrix-toolbar"><label>Source <select id="matrix-source" class="input">${sources.map((s) => `<option value="${s}" ${s === matrixSource ? "selected" : ""}>${sourceLabels[s]}</option>`).join("")}</select></label><span>${matrix.apps.length} apps · ${rows.length} tracked searches</span></div><div class="matrix-scroll" tabindex="0" role="region" aria-label="App visibility matrix. Scroll for more countries and apps."><table class="query-matrix"><thead><tr><th scope="col">COUNTRY / SEARCH</th>${matrix.apps.map((a) => `<th scope="col">${esc(a.name)}${a.id === ctx.appId() ? "<small>Selected app</small>" : ""}</th>`).join("")}</tr></thead><tbody>${rows.map(({ monitor, cells }) => `<tr><th scope="row"><strong>${esc(countryName(monitor.country))}</strong><small>${esc(monitor.language.toUpperCase())}${monitor.device ? " · " + esc(monitor.device) : ""}${store ? " · " + (monitor.source === "apple_app_store" ? "Up to " + monitor.depth * 50 + " results" : "Up to " + monitor.depth + " pages") : ""}${monitor.enabled ? "" : " · Paused"}</small></th>${cells.map(cell).join("")}</tr>`).join("")}</tbody></table></div><p class="insight-note">Each cell uses its latest saved check. Timestamps can differ. Select a cell to inspect its evidence. No additional SerpApi credits.</p>`,
      button(
        "Back to apps & competitors",
        "tracked-apps",
        "small",
        `data-id="${matrixMonitor}"`,
      ),
    );
    modal.dataset.view = "matrix";
  }

  async function notifications() {
    const request = ++requestVersion;
    showModal(
      "Notifications",
      "See alerts for major changes to your rank here.",
      '<div class="skeleton"></div>',
    );
    modal.dataset.view = "notifications";
    const data = await api("notifications");
    if (
      !modal.open ||
      modal.dataset.view !== "notifications" ||
      request !== requestVersion
    )
      return;
    showModal(
      "Notifications",
      "See alerts for major changes to your rank here.",
      data.notifications.length
        ? `<div class="notification-list">${data.notifications.map((n) => `<article class="notification-item ${n.read ? "" : "unread"}"><span class="notification-marker" ${n.read ? 'aria-hidden="true"' : 'role="img" aria-label="Unread notification"'}></span><div><strong>${esc(ctx.appById(n.app_id)?.name || "App")} ${n.data.after ? "lost ranking" : "left the checked results"}</strong><p>${esc(n.data.query)} · ${esc(countryName(n.data.country))}</p><div class="notification-ranks"><span>${rank(n.data.before)}</span><span>→</span><strong>${rank(n.data.after)}</strong><small>${sourceLabels[n.data.source]}</small></div><small class="muted">${when(n.created_at)}</small><div class="notification-actions">${button("View evidence", "notification-evidence", "small ghost", `data-id="${n.id}" data-run="${n.run_id}"`)}${!n.read ? button("Mark read", "notification-read", "small ghost", `data-id="${n.id}"`) : ""}${button("Dismiss", "notification-dismiss", "small ghost", `data-id="${n.id}"`)}</div></div></article>`).join("")}</div>`
        : '<div class="insight-empty"><span class="empty-emblem">✓</span><h3>No notifications</h3><p>If your app drops significantly in the rankings, you\'ll see an alert here. Open it to see which keyword and country were affected.</p></div>',
      data.notifications.length
        ? button("Mark all as read", "notifications-read-all", "small")
        : "",
    );
    modal.dataset.view = "notifications";
  }

  function listingPage() {
    const available = activeWatches();
    const watch = available.find((w) => w.id === selectedWatch);
    const totalCredits = watches.reduce((sum, w) => sum + w.monthly_credits, 0);
    return (
      ctx.heading(
        "Your listing, through time.",
        "See what changed in each store and country.",
        button(icon("plus") + " Track a listing", "listing-setup", "primary"),
        "LISTING HISTORY",
      ) +
      `<div class="listing-scope"><label>App<select class="input" id="app-filter"><option value="0">All apps</option>${ctx
        .state()
        .apps.filter((a) => !a.archived)
        .map(
          (a) =>
            `<option value="${a.id}" ${ctx.appId() === a.id ? "selected" : ""}>${esc(a.name)}</option>`,
        )
        .join(
          "",
        )}</select></label><label>Country<select class="input" id="listing-country"><option value="">All countries</option>${[
        ...new Set(
          watches
            .filter((w) => !ctx.appId() || w.app_id === ctx.appId())
            .map((w) => w.country),
        ),
      ]
        .sort()
        .map(
          (country) =>
            `<option value="${country}" ${ctx.countryFilter() === country ? "selected" : ""}>${esc(countryName(country))}</option>`,
        )
        .join(
          "",
        )}</select></label><span class="listing-cost">${totalCredits ? `~${Math.round(totalCredits)} credits / month across enabled listings` : "Tracking is off until you enable a listing"}</span></div>` +
      (!watch
        ? `<section class="panel listing-empty"><div class="listing-preview" aria-hidden="true"><div><span>Before</span><i></i><i></i><i></i></div><div><span>After</span><i></i><i class="added"></i><i></i></div></div><h2>Track changes to your store listing.</h2><p>Follow titles, descriptions, icons and screenshots. Enable one app, store and country at a time.</p><p class="insight-note">Each check uses a SerpApi credit. You choose the frequency before tracking starts.</p>${button("Choose a listing", "listing-setup", "primary")}</section>`
        : `<section class="panel listing-workspace"><div class="listing-workspace-head"><label class="watch-select">Tracked listing<select class="input" id="listing-watch">${available.map((w) => `<option value="${w.id}" ${w.id === selectedWatch ? "selected" : ""}>${esc(w.app_name)} · ${w.platform === "ios" ? "App Store" : "Google Play"} · ${esc(countryName(w.country))}${w.language === "auto" ? "" : " · " + esc(w.language.toUpperCase())}</option>`).join("")}</select></label><div class="watch-actions"><span class="tag ${watch.enabled && !watch.archived ? "good" : "neutral"}">${watch.archived ? "Archived" : watch.enabled ? "Tracking" : "Paused"}</span>${button("Manage", "listing-manage", "small", `data-id="${watch.id}"`)}</div></div>
      <div class="listing-status"><span>${watch.enabled && !watch.archived ? `${frequencies[watch.frequency]} · ~${Math.round(watch.monthly_credits)} credits/month · Next ${when(watch.next_run_at)}` : "Collection paused. Your saved history is retained."}</span><span>${watch.status === "queued" ? "Check queued" : watch.status === "running" ? "Checking listing…" : `Last checked ${when(watch.checked_at)}`}</span></div>
      ${watch.status === "error" ? `<div class="notice error">Check failed: ${esc(watch.error)}. The last saved snapshot is unchanged.</div>` : ""}
      <div class="timeline-heading"><h2>Changes</h2><label class="toggle-label"><input type="checkbox" id="listing-unchanged" ${unchanged ? "checked" : ""}>Show unchanged checks</label></div>
      <div class="listing-timeline">${timeline.snapshots.length ? timeline.snapshots.map((s) => `<button class="timeline-event" data-action="listing-snapshot" data-id="${s.id}"><span class="timeline-dot ${s.baseline ? "baseline" : ""}"></span><time>${when(s.checked_at)}</time><div><strong>${s.baseline ? "First snapshot" : s.changes.length ? `${s.changes.length} ${s.changes.length === 1 ? "field" : "fields"} changed` : "No changes"}</strong><p>${s.baseline ? "Your starting point for future comparisons" : s.changes.map(labelField).join(" · ") || "The listing matches the previous check"}</p></div><span class="timeline-open">${s.baseline ? "View" : "Compare"} ${icon("arrow")}</span></button>`).join("") : `<div class="insight-empty"><h3>${!watch.enabled || watch.archived ? "No snapshots yet" : watch.status === "error" ? "Waiting for a successful baseline" : "Your first snapshot is on its way"}</h3><p>Changes will appear after the listing has been checked successfully.</p></div>`}</div>${timeline.next_cursor ? button("Load older changes", "listing-older", "small", `data-cursor="${timeline.next_cursor}"`) : ""}</section>`)
    );
  }

  function listingSetup() {
    const apps = ctx.state().apps.filter((a) => !a.archived);
    const selected = apps.find((a) => a.id === ctx.appId()) || apps[0];
    if (!selected) {
      ctx.toast("Connect an app before enabling listing history.", true);
      return;
    }
    showModal(
      "Track a listing",
      "Choose the store, country, and check frequency.",
      `<form id="listing-setup-form"><div class="form-grid"><label class="field"><span>App</span><select name="app_id" id="history-app">${apps.map((a) => `<option value="${a.id}" ${a.id === selected.id ? "selected" : ""}>${esc(a.name)}</option>`).join("")}</select></label><label class="field"><span>Store</span><select name="listing_id" id="history-listing"></select></label><label class="field"><span>Country</span><select name="country" id="history-country"></select></label><label class="field" id="history-language-field"><span>Language</span><input class="input" name="language" value="en" pattern="[a-z]{2,3}(-[a-z0-9]{2,3})?" maxlength="12" required><small>Google Play language code, e.g. en, de, pt-br.</small></label><label class="field"><span>Update frequency</span><select name="frequency">${Object.entries(
        frequencies,
      )
        .map(
          ([key, label]) =>
            `<option value="${key}" ${key === "weekly" ? "selected" : ""}>${label}</option>`,
        )
        .join(
          "",
        )}</select></label></div><div class="notice" id="listing-estimate"></div><p class="insight-note">The first check saves a baseline. Tracking starts immediately and continues while AppTrail is running. You can pause it at any time. Images are saved locally when available.</p><div class="form-footer"><button class="button primary" type="submit" ${ctx.state().configured ? "" : "disabled"}>Enable & save first snapshot</button></div>${ctx.state().configured ? "" : '<p class="error-message">Connect SerpApi in Settings first.</p>'}</form>`,
    );
    fillHistoryListings();
  }
  function fillHistoryListings() {
    const form = document.querySelector("#listing-setup-form");
    const app = ctx.appById(Number(form.elements.app_id.value));
    form.elements.listing_id.innerHTML = app.listings
      .map(
        (l) =>
          `<option value="${l.id}">${l.platform === "ios" ? "App Store" : "Google Play"}</option>`,
      )
      .join("");
    fillHistoryCountries();
  }
  function fillHistoryCountries() {
    const form = document.querySelector("#listing-setup-form");
    const listing = ctx
      .state()
      .apps.flatMap((a) => a.listings)
      .find((l) => l.id === Number(form.elements.listing_id.value));
    const regions = ctx.regions(),
      source = listing.platform === "ios" ? "apple_app_store" : "google_play";
    const countries =
      regions[source] ||
      regions[listing.platform === "ios" ? "apple" : "google"];
    const selected = [
      ctx.countryFilter(),
      listing.country,
      "us",
      countries[0],
    ].find((country) => countries.includes(country));
    form.elements.country.innerHTML = countries
      .map(
        (c) =>
          `<option value="${c}" ${c === selected ? "selected" : ""}>${esc(countryName(c))}</option>`,
      )
      .join("");
    document.querySelector("#history-language-field").hidden =
      listing.platform === "ios";
    form.elements.language.value =
      listing.platform === "ios" ? "auto" : listing.language || "en";
    form.elements.language.disabled = listing.platform === "ios";
    updateEstimate();
  }
  function updateEstimate() {
    const form = document.querySelector("#listing-setup-form");
    if (!form) return;
    const credits = Math.round(30 / intervals[form.elements.frequency.value]);
    document.querySelector("#listing-estimate").innerHTML =
      `<strong>Uses SerpApi credits</strong><br>1 credit for the first snapshot, then approximately ${credits} credits per month for this store and country. Manual checks and retries are extra.`;
  }
  function manageWatch(id) {
    const w = watches.find((item) => item.id === id);
    showModal(
      "Listing schedule",
      `${esc(w.app_name)} · ${w.platform === "ios" ? "App Store" : "Google Play"} · ${esc(countryName(w.country))}`,
      `<form id="listing-manage-form" data-id="${id}"><label class="field"><span>Update frequency</span><select name="frequency" id="manage-frequency">${Object.entries(
        frequencies,
      )
        .map(
          ([key, label]) =>
            `<option value="${key}" ${key === w.frequency ? "selected" : ""}>${label}</option>`,
        )
        .join(
          "",
        )}</select></label><p class="notice" id="manage-estimate">Approximately ${Math.round(30 / intervals[w.frequency])} SerpApi credits per month while enabled. Manual checks and retries are extra.</p><div class="form-footer"><button class="button primary" type="submit">Save frequency</button></div></form><div class="separator"></div><div class="watch-manage-actions">${button(w.enabled ? "Pause tracking" : "Resume · 1 credit now", "listing-toggle", "", `data-id="${id}" ${w.archived ? "disabled" : ""}`)}${w.enabled ? button("Check now · 1 credit", "listing-check", "", `data-id="${id}" ${w.archived ? "disabled" : ""}`) : ""}</div><p class="insight-note">Pausing keeps every saved snapshot.</p>`,
    );
  }

  function mediaImage(item, alt, snapshot) {
    if (item?.asset_id && !snapshot?.missing_assets?.includes(item.asset_id))
      return `<img src="/api/listing-assets/${esc(item.asset_id)}" alt="${esc(alt)}" loading="lazy">`;
    const reason = item?.asset_id && snapshot?.images_omitted
      ? "Image omitted from backup to save space"
      : item?.asset_id && snapshot?.images_cleaned
        ? "Image deleted during storage cleanup"
        : "Archived image unavailable";
    return `<div class="unavailable-image">${reason}</div>`;
  }
  async function showSnapshot(id) {
    const request = ++requestVersion;
    showModal(
      "Listing comparison",
      "Loading saved snapshots…",
      '<div class="skeleton"></div>',
    );
    modal.dataset.view = "listing-diff";
    const { snapshot: after, previous: before } = await api(
      `listing-snapshots/${id}`,
    );
    if (
      !modal.open ||
      modal.dataset.view !== "listing-diff" ||
      request !== requestVersion
    )
      return;
    const fields = before
      ? after.changes
      : Object.keys(after.data).filter(
          (f) =>
            after.data[f] &&
            (!Array.isArray(after.data[f]) || after.data[f].length),
        );
    function fieldView(field) {
      const a = before?.data[field],
        b = after.data[field];
      let left, right;
      if (field === "icon") {
        left = a
          ? mediaImage(a, "Previous app icon", before)
          : '<span class="muted">No previous icon</span>';
        right = b
          ? mediaImage(b, "Current app icon", after)
          : '<span class="muted">No icon</span>';
      } else if (field.endsWith("screenshots")) {
        const old = a || [],
          next = b || [],
          annotated = mediaChanges(old, next);
        left = `<div class="screenshot-strip">${old.map((item, i) => `<figure>${mediaImage(item, `Previous screenshot ${i + 1}`, before)}<figcaption>${i + 1} · ${next.some((n) => sameMedia(n, item)) ? "Previous" : "Removed"}</figcaption></figure>`).join("") || '<span class="muted">No screenshots</span>'}</div>`;
        right = `<div class="screenshot-strip">${annotated.map((item, i) => `<figure>${mediaImage(item, `Current screenshot ${i + 1}`, after)}<figcaption class="${item.label === "Added" ? "up" : ""}">${i + 1} · ${item.label}</figcaption></figure>`).join("") || '<span class="muted">No screenshots</span>'}</div>`;
      } else {
        const diff = textDiff(a ?? "", b ?? "");
        const text = (parts, tag) =>
          parts
            .map((p) =>
              p.changed ? `<${tag}>${esc(p.text)}</${tag}>` : esc(p.text),
            )
            .join("") || '<span class="muted">Not provided by the store</span>';
        left = `<div class="diff-text">${text(diff.before, "del")}</div>`;
        right = `<div class="diff-text">${text(diff.after, "ins")}</div>`;
      }
      return `<section class="diff-field ${field === "icon" ? "icon-diff" : ""}"><h3>${esc(labelField(field))}</h3><div class="diff-columns ${before ? "" : "baseline-only"}">${before ? `<div class="diff-before"><span class="diff-mobile-label">Before</span>${left}</div>` : ""}<div class="diff-after"><span class="diff-mobile-label">${before ? "After" : "Baseline"}</span>${right}</div></div></section>`;
    }
    showModal(
      after.baseline ? "First listing snapshot" : "What changed",
      before
        ? `${when(before.checked_at)} → ${when(after.checked_at)}`
        : when(after.checked_at),
      `${before?.images_omitted || after.images_omitted ? '<p class="notice backup-omission">Listing-history images were omitted from this backup to save space. Saved text and image-change records are still available.</p>' : before?.images_cleaned || after.images_cleaned ? '<p class="notice storage-omission">Older listing-history images were deleted during storage cleanup. Saved text and image-change records are still available.</p>' : ""}<div class="diff-legend">${before ? '<span><i class="removed-key"></i>Removed</span><span><i class="added-key"></i>Added</span>' : "Baseline saved. Future changes will be compared with this listing."}<span>Saved listing history</span></div>${before ? `<div class="diff-column-head"><span>BEFORE <small>${when(before.checked_at)}</small></span><span>AFTER <small>${when(after.checked_at)}</small></span></div>` : ""}${fields.map(fieldView).join("") || '<div class="insight-empty">No listing changes in this check.</div>'}`,
    );
    modal.dataset.view = "listing-diff";
  }

  async function click(el) {
    const action = el.dataset.action,
      id = Number(el.dataset.id);
    if (action === "matrix") await showMatrix(id);
    else if (action === "notifications") await notifications();
    else if (action === "notifications-read-all") {
      await api("notifications/read", { method: "POST" });
      await ctx.sync();
      if (modal.open && modal.dataset.view === "notifications")
        await notifications();
    } else if (action.startsWith("notification-")) {
      await api(`notifications/${id}`, {
        method: "PATCH",
        body: {
          action: action === "notification-dismiss" ? "dismiss" : "read",
        },
      });
      await ctx.sync();
      if (action === "notification-evidence") {
        if (modal.open && modal.dataset.view === "notifications")
          await ctx.showRun(Number(el.dataset.run));
      } else if (modal.open && modal.dataset.view === "notifications")
        await notifications();
    } else if (action === "distribution-bucket") {
      const entries = distribution.entries.filter(
        (r) => r.bucket === el.dataset.bucket,
      );
      showModal(
        bucketLabels[el.dataset.bucket],
        `${entries.length} app / keyword / country combinations`,
        entries.length
          ? `<div class="distribution-entries">${entries.map((r) => `<button class="movement-row" data-action="run" data-id="${r.run_id}"><span><strong>${esc(r.params.query)}</strong><small>${esc(ctx.appById(r.app_id)?.name)} · ${esc(countryName(r.params.country))} · ${sourceLabels[r.params.source]}</small></span><strong>${rank(r.data.position)}</strong></button>`).join("")}</div>`
          : '<div class="insight-empty">No queries in this group.</div>',
      );
    } else if (action === "listing-setup") listingSetup();
    else if (action === "listing-manage") manageWatch(id);
    else if (action === "listing-snapshot") await showSnapshot(id);
    else if (action === "listing-older") {
      const more = await api(
        `listing-watches/${selectedWatch}/history?include_unchanged=${unchanged}&before_id=${el.dataset.cursor}`,
      );
      timeline = {
        snapshots: [...timeline.snapshots, ...more.snapshots],
        next_cursor: more.next_cursor,
      };
      ctx.render();
    } else if (action === "listing-toggle") {
      const watch = watches.find((w) => w.id === id);
      el.disabled = true;
      await api(`listing-watches/${id}`, {
        method: "PATCH",
        body: { enabled: !watch.enabled },
      });
      modal.close();
      await ctx.sync();
    } else if (action === "listing-check") {
      el.disabled = true;
      await api(`listing-watches/${id}/check`, { method: "POST" });
      modal.close();
      await ctx.sync();
    } else return false;
    return true;
  }
  async function change(el) {
    if (el.id === "matrix-source") {
      matrixSource = el.value;
      renderMatrix();
    } else if (el.id === "listing-watch") {
      selectedWatch = Number(el.value);
      await ctx.sync();
    } else if (el.id === "listing-unchanged") {
      unchanged = el.checked;
      await ctx.sync();
    } else if (el.id === "listing-country") {
      ctx.setCountry(el.value);
      await ctx.sync();
    } else if (el.id === "history-app") fillHistoryListings();
    else if (el.id === "history-listing") fillHistoryCountries();
    else if (el.closest("#listing-setup-form")) updateEstimate();
    else if (el.id === "manage-frequency")
      document.querySelector("#manage-estimate").textContent =
        `Approximately ${Math.round(30 / intervals[el.value])} SerpApi credits per month while enabled. Manual checks and retries are extra.`;
    else return false;
    return true;
  }
  async function submit(form) {
    const data = new FormData(form);
    if (form.id === "listing-setup-form") {
      const result = await api("listing-watches", {
        method: "POST",
        body: {
          listing_id: Number(data.get("listing_id")),
          country: data.get("country"),
          language: data.get("language") || "auto",
          frequency: data.get("frequency"),
        },
      });
      selectedWatch = result.id;
      ctx.setCountry("");
      ctx.setApp(Number(data.get("app_id")));
      modal.close();
      await ctx.sync();
    } else if (form.id === "listing-manage-form") {
      await api(`listing-watches/${form.dataset.id}`, {
        method: "PATCH",
        body: { frequency: data.get("frequency") },
      });
      modal.close();
      await ctx.sync();
    } else return false;
    return true;
  }
  return {
    load,
    apply,
    distributionPanel,
    listingPage,
    click,
    change,
    submit,
    showSnapshot,
  };
}
