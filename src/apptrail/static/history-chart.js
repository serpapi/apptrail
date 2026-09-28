const storeSources = new Set(["apple_app_store", "google_play"]);

export function localDateValue(date = new Date()) {
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
  ].join("-");
}

export function visibilitySeries(rows, options = {}) {
  const {
    kind = "rank",
    groupBy = "source",
    source = "",
    query = "",
    countries = [],
  } = options;
  const filtered = rows.filter(
    (row) =>
      (kind === "rank") === storeSources.has(row.params.source) &&
      (!source || row.params.source === source) &&
      (!query || row.params.query === query) &&
      (groupBy !== "country" ||
        (row.params.country !== "global" &&
          countries.includes(row.params.country))),
  );
  const dayOf = (row) =>
    new Date(row.checked_at * 1000).toISOString().slice(0, 10);
  const observedDates = [...new Set(filtered.map(dayOf))].sort();
  const dates = [];
  if (observedDates.length) {
    const last = Date.parse(observedDates.at(-1) + "T00:00:00Z");
    for (
      let day = Date.parse(observedDates[0] + "T00:00:00Z");
      day <= last;
      day += 86400000
    ) {
      dates.push(new Date(day).toISOString().slice(0, 10));
    }
  }
  const groups =
    groupBy === "country"
      ? countries
      : [...new Set(filtered.map((row) => row.params.source))];
  const daily = new Map();
  for (const row of filtered) {
    if (row.status !== "success") continue;
    if (kind === "rank") {
      if (!Number.isFinite(row.data.position) || row.data.position <= 0)
        continue;
    } else if (row.data.answer_available === false) continue;
    const key = `${row.params[groupBy]}:${dayOf(row)}`;
    const value = daily.get(key) || { sum: 0, count: 0 };
    value.sum +=
      kind === "rank" ? row.data.position : Number(Boolean(row.data.mentioned));
    value.count++;
    daily.set(key, value);
  }
  const series = groups.map((key) => ({
    key,
    data: dates.map((day) => {
      const value = daily.get(`${key}:${day}`);
      if (!value) return null;
      return kind === "rank"
        ? value.sum / value.count
        : Math.round((value.sum / value.count) * 100);
    }),
  }));
  return { dates, series };
}
