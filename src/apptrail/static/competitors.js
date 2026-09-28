const storeNames = { ios: "App Store", android: "Google Play" };
const platformFor = (source) =>
  ({ apple_app_store: "ios", google_play: "android" })[source];
const queryKey = (monitor) => JSON.stringify([monitor.source, monitor.query]);

export function competitorQueryGroups(monitors) {
  const groups = new Map();
  for (const monitor of monitors) {
    const key = queryKey(monitor);
    if (!groups.has(key))
      groups.set(key, {
        key,
        query: monitor.query,
        source: monitor.source,
        monitors: [],
      });
    groups.get(key).monitors.push(monitor);
  }
  return [...groups.values()].sort(
    (a, b) =>
      a.query.localeCompare(b.query) || a.source.localeCompare(b.source),
  );
}

export function selectedCompetitorMonitors(monitors, queries, countries) {
  return monitors.filter(
    (monitor) =>
      queries.has(queryKey(monitor)) && countries.has(monitor.country),
  );
}

export function createCompetitorUI(ctx) {
  const { esc, icon, button, api, countryName, sourceLabels } = ctx;
  let draft = null;
  const apps = () => ctx.state().apps.filter((app) => !app.archived);
  const originalMonitors = () =>
    ctx
      .state()
      .monitors.filter((monitor) => monitor.app_ids.includes(draft.parentId));
  const selected = () =>
    selectedCompetitorMonitors(
      originalMonitors(),
      draft.queries,
      draft.countries,
    );
  const existing = () => apps().find((app) => app.id === draft.existingId);
  const platforms = () =>
    draft.mode === "existing"
      ? (existing()?.listings || []).map((listing) => listing.platform)
      : Object.keys(draft.listings);

  function selectParent(id) {
    draft.parentId = id;
    const monitors = originalMonitors();
    draft.queries = new Set(monitors.map(queryKey));
    draft.countries = new Set(monitors.map((monitor) => monitor.country));
    if (draft.existingId === id) draft.existingId = 0;
  }

  function open(parentId = 0, existingId = 0) {
    const active = apps();
    if (!active.length) return;
    const activeIds = new Set(active.map((app) => app.id));
    draft = {
      parentId: 0,
      existingId,
      mode: existingId ? "existing" : "new",
      name: "",
      aliases: "",
      website: "",
      country: "us",
      language: "en",
      listings: {},
      search: { ios: "", android: "" },
      results: {},
      requests: { ios: 0, android: 0 },
      loading: {},
      errors: {},
      saving: false,
    };
    selectParent(
      active.find((app) => app.id === parentId)?.id ||
        active.find(
          (app) => !app.competitor_for?.some((id) => activeIds.has(id)),
        )?.id ||
        active[0].id,
    );
    ctx.modal.close();
    location.hash = "apps";
    ctx.render();
    document.querySelector("#competitor-parent")?.focus();
  }

  function issue() {
    if (!apps().some((app) => app.id === draft.parentId))
      return "Choose an active app to compare with.";
    if (
      draft.mode === "existing" &&
      (!existing() || draft.existingId === draft.parentId)
    )
      return "Choose a competitor from your workspace.";
    if (!platforms().length) return "Connect at least one store listing.";
    if (draft.mode === "new" && !draft.name.trim())
      return "Enter a name for the competitor.";
    if (originalMonitors().length && !selected().length)
      return "Select at least one query and its country.";
    const missing = [
      ...new Set(
        selected()
          .map((monitor) => platformFor(monitor.source))
          .filter((platform) => platform && !platforms().includes(platform)),
      ),
    ];
    return missing.length
      ? `Connect ${missing.map((platform) => storeNames[platform]).join(" and ")} or deselect those store queries.`
      : "";
  }

  function summary() {
    const monitors = selected(),
      store = monitors.filter((monitor) => platformFor(monitor.source)).length,
      regional = new Set(
        monitors
          .map((monitor) => monitor.country)
          .filter((country) => country !== "global"),
      ),
      global = monitors.some((monitor) => monitor.country === "global");
    return `${store} store searches · ${monitors.length - store} AI searches · ${regional.size} ${regional.size === 1 ? "country" : "countries"}${global ? " + Global" : ""}`;
  }

  function updateSummary() {
    if (!draft || !document.querySelector("#competitor-save-form")) return;
    document.querySelector("#competitor-selection-summary").textContent =
      summary();
    document.querySelector("#competitor-selection-issue").textContent = issue();
    const submit = document.querySelector("#competitor-save");
    submit.disabled = draft.saving || !!issue();
    document
      .querySelectorAll("[data-competitor-select-all]")
      .forEach((control) => {
        const options = [
          ...document.querySelectorAll(
            `[data-competitor-group="${control.dataset.competitorSelectAll}"]`,
          ),
        ];
        control.textContent =
          options.length && options.every((option) => option.checked)
            ? "Clear all"
            : "Select all";
      });
  }

  function storePicker(platform) {
    const listing = draft.listings[platform];
    return `<section class="competitor-store"><div class="competitor-store-heading">${icon(platform === "ios" ? "smartphone" : "play")}<h3>${storeNames[platform]}</h3><span class="tag ${listing ? "good" : "neutral"}">${listing ? "Selected" : "Optional"}</span></div>${
      listing
        ? `<div class="competitor-picked"><strong>${esc(listing.title)}</strong><span>${esc(listing.developer)}</span><small>${esc(listing.external_id)}</small>${button("Change listing", "competitor-setup-clear-listing", "small ghost", `data-platform="${platform}" type="button"`)}</div>`
        : `<form id="competitor-discover-${platform}" data-platform="${platform}"><label class="field"><span>Find the ${platform === "ios" ? "iOS" : "Android"} app</span><input class="input" name="query" data-competitor-search="${platform}" value="${esc(draft.search[platform])}" placeholder="App name or store URL" required maxlength="500"></label><button class="button small" type="submit" ${draft.loading[platform] ? "disabled" : ""}>${icon("search")} ${draft.loading[platform] ? "Searching…" : "Search"}</button></form><div class="competitor-discovery-results">${(draft.results[platform] || []).map((candidate, index) => `<button type="button" class="competitor-discovery-result" data-action="competitor-setup-pick" data-platform="${platform}" data-index="${index}"><span><strong>${esc(candidate.title)}</strong><small>${esc(candidate.developer)} · ${esc(candidate.external_id)}</small></span>${icon("plus")}</button>`).join("")}</div>${draft.results[platform]?.length === 0 ? '<p class="muted">No matches. Try another name or a store URL.</p>' : ""}`
    }<p class="error-message" role="alert">${esc(draft.errors[platform] || "")}</p></section>`;
  }

  function querySection(kind, title, groups) {
    return `<section class="competitor-query-section"><div class="competitor-section-heading"><h3>${title}</h3>${groups.length ? `<button type="button" class="text-link" data-action="competitor-setup-select-all" data-competitor-select-all="${kind}">Select all</button>` : ""}</div><div class="competitor-query-options">${groups.map((group) => `<label class="competitor-query-option"><input type="checkbox" data-competitor-query="${esc(group.key)}" data-competitor-group="${kind}" ${draft.queries.has(group.key) ? "checked" : ""}><span><strong>${esc(group.query)}</strong><small>${sourceLabels[group.source]} · ${[...new Set(group.monitors.map((monitor) => countryName(monitor.country)))].map(esc).join(", ")}${group.monitors.every((monitor) => !monitor.enabled) ? " · Paused" : ""}</small></span></label>`).join("") || '<p class="muted">No saved queries yet.</p>'}</div></section>`;
  }

  function page() {
    const groups = competitorQueryGroups(originalMonitors()),
      countries = [
        ...new Set(originalMonitors().map((monitor) => monitor.country)),
      ].sort((a, b) => countryName(a).localeCompare(countryName(b))),
      choices = apps().filter((app) => app.id !== draft.parentId),
      lookupCountries = ctx
        .sourceCountries("apple_app_store")
        .filter((country) =>
          ctx.sourceCountries("google_play").includes(country),
        );
    return (
      ctx.heading(
        "Add a competitor",
        "Compare another app across your saved searches and markets.",
        button("Cancel", "competitor-setup-cancel"),
        "APPS AND COMPETITORS",
      ) +
      `<fieldset class="competitor-editor" ${draft.saving ? "disabled" : ""}>
      <section class="panel competitor-section"><div class="competitor-section-heading"><h2><span class="competitor-step">1</span> Choose your app</h2></div><div class="field competitor-parent-field"><label class="field-label" for="competitor-parent">Compare against</label><select class="input" id="competitor-parent">${apps()
        .map(
          (app) =>
            `<option value="${app.id}" ${app.id === draft.parentId ? "selected" : ""}>${esc(app.name)}</option>`,
        )
        .join(
          "",
        )}</select><small>The competitor will be labeled with this app’s name.</small></div></section>
      <section class="panel competitor-section"><div class="competitor-section-heading"><h2><span class="competitor-step">2</span> Connect the competitor</h2></div><div class="competitor-mode" role="group" aria-label="Choose competitor"><button type="button" class="chip ${draft.mode === "new" ? "active" : ""}" data-action="competitor-setup-mode" data-mode="new" aria-pressed="${draft.mode === "new"}">Find a new app</button><button type="button" class="chip ${draft.mode === "existing" ? "active" : ""}" data-action="competitor-setup-mode" data-mode="existing" aria-pressed="${draft.mode === "existing"}">Use a workspace app</button></div>${
        draft.mode === "existing"
          ? `<div class="field"><label class="field-label" for="competitor-existing">Competitor app</label><select class="input" id="competitor-existing"><option value="0">Choose an app</option>${choices.map((app) => `<option value="${app.id}" ${app.id === draft.existingId ? "selected" : ""}>${esc(app.name)} · ${app.listings.map((listing) => (listing.platform === "ios" ? "iOS" : "Android")).join(" + ")}</option>`).join("")}</select></div>${
              existing()
                ? `<div class="competitor-existing-stores">${existing()
                    .listings.map(
                      (listing) =>
                        `<span class="tag good">${icon("check")} ${storeNames[listing.platform]} connected</span>`,
                    )
                    .join(
                      "",
                    )}${existing().listings.length < 2 ? button(icon("plus") + " Link the other store", "link-store", "small", `data-id="${draft.existingId}"`) : ""}</div>`
                : '<p class="muted">Choose an existing app to reuse its connected listings.</p>'
            }`
          : `<p class="competitor-section-copy">Connect one or both stores. Each selected store search needs a matching listing.</p>${!ctx.state().configured ? '<p class="notice">Connect SerpApi in <a class="text-link" href="#settings">Settings</a> to find and verify store listings.</p>' : ""}<div class="field-row competitor-lookup-options"><label class="field"><span>Store lookup country</span><select class="input" data-competitor-field="country">${ctx.countries(draft.country, lookupCountries)}</select></label><label class="field"><span>Store lookup language</span><select class="input" data-competitor-field="language">${ctx.languages(draft.language)}</select></label></div><div class="competitor-store-grid">${storePicker("ios")}${storePicker("android")}</div><p class="competitor-credit-note">Finding and verifying store listings uses SerpApi credits.</p><div class="field"><label class="field-label" for="competitor-name">Competitor name in your workspace</label><input class="input" id="competitor-name" form="competitor-save-form" data-competitor-field="name" value="${esc(draft.name)}" maxlength="160" required placeholder="e.g. TickTick" aria-describedby="competitor-name-help"><small id="competitor-name-help">AI matching uses this name and your aliases. Include the short product name if the store title contains extra words.</small></div><div class="field-row"><label class="field"><span>Other names (optional)</span><input class="input" form="competitor-save-form" data-competitor-field="aliases" value="${esc(draft.aliases)}" placeholder="Comma-separated aliases"></label><label class="field"><span>App website (optional)</span><input class="input" type="url" form="competitor-save-form" data-competitor-field="website" value="${esc(draft.website)}" maxlength="500" placeholder="https://yourapp.com" aria-describedby="competitor-website-help"><small id="competitor-website-help">Used to track AI citations linking to your app’s website.</small></label></div>`
      }</section>
      <section class="panel competitor-section"><div class="competitor-section-heading"><h2><span class="competitor-step">3</span> Choose queries and countries</h2></div><p class="competitor-section-copy">All saved searches start selected. Choose the keywords, AI questions and countries to compare. Existing schedules stay the same.</p>${
        groups.length
          ? `<div class="competitor-country-section"><div class="competitor-section-heading"><h3>Countries</h3><button type="button" class="text-link" data-action="competitor-setup-select-all" data-competitor-select-all="countries">Select all</button></div><div class="competitor-countries">${countries.map((country) => `<label class="check-chip"><input type="checkbox" data-competitor-country="${esc(country)}" data-competitor-group="countries" ${draft.countries.has(country) ? "checked" : ""}>${esc(countryName(country))}</label>`).join("")}</div></div><div class="competitor-query-grid">${querySection(
              "store",
              "Store queries",
              groups.filter((group) => platformFor(group.source)),
            )}${querySection(
              "ai",
              "AI questions",
              groups.filter((group) => !platformFor(group.source)),
            )}</div>`
          : '<div class="notice">This app has no saved queries yet. You can add the competitor now and choose searches later from Apps and competitors.</div>'
      }</section>
      <form id="competitor-save-form" class="panel competitor-save"><div><strong id="competitor-selection-summary">${summary()}</strong><p>Reuse saved results and future checks. No extra scheduled searches.</p><p class="competitor-selection-issue" id="competitor-selection-issue">${esc(issue())}</p><p class="error-message" id="competitor-save-error" role="alert">${esc(draft.errors.save || "")}</p></div><button class="button primary" id="competitor-save" type="submit" ${issue() || draft.saving ? "disabled" : ""}>${draft.saving ? "Adding competitor…" : "Add competitor"}${icon("arrow")}</button></form></fieldset>`
    );
  }

  async function submit(form) {
    if (
      !draft ||
      (!form.id.startsWith("competitor-discover-") &&
        form.id !== "competitor-save-form")
    )
      return false;
    const current = draft;
    if (form.id.startsWith("competitor-discover-")) {
      const platform = form.dataset.platform,
        request = ++current.requests[platform];
      current.loading[platform] = true;
      current.errors[platform] = "";
      ctx.render();
      try {
        const result = await api("discover", {
          method: "POST",
          body: {
            platform,
            query: current.search[platform],
            country: current.country,
            language: current.language,
          },
        });
        if (draft === current && current.requests[platform] === request)
          current.results[platform] = result.candidates;
      } catch (error) {
        if (draft === current && current.requests[platform] === request)
          current.errors[platform] = error.message;
      } finally {
        if (draft === current && current.requests[platform] === request) {
          current.loading[platform] = false;
          ctx.render();
        }
      }
    } else {
      if (current.saving || issue()) {
        updateSummary();
        return true;
      }
      current.saving = true;
      current.errors.save = "";
      const body = { monitor_ids: selected().map((monitor) => monitor.id) };
      if (current.mode === "existing")
        body.existing_app_id = current.existingId;
      else
        body.app = {
          name: current.name.trim(),
          aliases: current.aliases
            .split(",")
            .map((value) => value.trim())
            .filter(Boolean),
          website: current.website.trim(),
          candidate_tokens: Object.values(current.listings).map(
            (listing) => listing.token,
          ),
        };
      ctx.render();
      try {
        await api(`apps/${current.parentId}/competitors`, {
          method: "POST",
          body,
        });
        if (draft === current) draft = null;
        await ctx.sync();
        ctx.toast("Competitor added to the selected searches.");
      } catch (error) {
        if (draft === current) {
          current.saving = false;
          current.errors.save = error.message;
          ctx.render();
        }
      }
    }
    return true;
  }

  function click(el) {
    const action = el.dataset.action;
    if (action === "add-competitor") {
      open(
        Number(el.dataset.appId || ctx.appId()),
        Number(el.dataset.existingId || 0),
      );
      return true;
    }
    if (!action.startsWith("competitor-setup-") || !draft) return false;
    if (draft.saving) return true;
    if (action === "competitor-setup-cancel") draft = null;
    if (action === "competitor-setup-mode") draft.mode = el.dataset.mode;
    if (action === "competitor-setup-pick") {
      const platform = el.dataset.platform,
        listing = draft.results[platform]?.[Number(el.dataset.index)];
      if (listing) {
        draft.listings[platform] = listing;
        if (!draft.name) draft.name = listing.title.split(":")[0];
      }
    }
    if (action === "competitor-setup-clear-listing")
      delete draft.listings[el.dataset.platform];
    if (action === "competitor-setup-select-all") {
      const options = [
        ...document.querySelectorAll(
          `[data-competitor-group="${el.dataset.competitorSelectAll}"]`,
        ),
      ];
      const checked = !options.every((option) => option.checked);
      for (const option of options) {
        option.checked = checked;
        const country = option.dataset.competitorCountry,
          key = country || option.dataset.competitorQuery;
        (country ? draft.countries : draft.queries)[checked ? "add" : "delete"](
          key,
        );
      }
      updateSummary();
      return true;
    }
    ctx.render();
    return true;
  }

  function input(el) {
    if (!draft || draft.saving) return;
    if (el.dataset.competitorSearch)
      draft.search[el.dataset.competitorSearch] = el.value;
    if (el.dataset.competitorField)
      draft[el.dataset.competitorField] = el.value;
    updateSummary();
  }

  function change(el) {
    if (!draft || draft.saving) return;
    if (el.id === "competitor-parent") {
      selectParent(Number(el.value));
      ctx.render();
    }
    if (el.id === "competitor-existing") {
      draft.existingId = Number(el.value);
      ctx.render();
    }
    if (el.dataset.competitorCountry)
      draft.countries[el.checked ? "add" : "delete"](
        el.dataset.competitorCountry,
      );
    if (el.dataset.competitorQuery)
      draft.queries[el.checked ? "add" : "delete"](el.dataset.competitorQuery);
    if (["country", "language"].includes(el.dataset.competitorField)) {
      draft[el.dataset.competitorField] = el.value;
      for (const platform of Object.keys(storeNames)) {
        draft.requests[platform]++;
        draft.loading[platform] = false;
        delete draft.results[platform];
        delete draft.errors[platform];
      }
      ctx.render();
    }
    updateSummary();
  }

  return {
    active: () => !!draft,
    page,
    open,
    submit,
    click,
    input,
    change,
    updateSummary,
  };
}
