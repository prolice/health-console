// The dense view: current readings, then history over one window shared by
// every chart, then the material of last resort.
//
// The tile row deliberately carries no ARIA "live region" marker: it
// repaints on every SSE event (every 2s), and one there would be a screen
// reader talking without pause. redrawCharts() must not otherwise run from
// that 2s tick -- it runs on a range change, the refresh button, a theme
// change, entering Expert mode, and one deliberate exception on the tick
// itself: renderExpert() triggers it once when the discovered thermal
// zones change since the charts were last drawn (see
// thermalZoneSignature()), which a cheap signature comparison keeps from
// firing on any tick where the zone set is unchanged.

import { el, clear } from "./dom.js";
import { translate, formatNumber, formatBytes } from "./i18n.js";
import { RANGES, fetchSeries } from "./history.js";
import { chartsAvailable, drawLine, describeSeries, labelChart,
         themeColour, destroyIn, metricLabel } from "./charts.js";
import { lastKnownState } from "./stream.js";

const DEFAULT_RANGE = "24h";
let range = DEFAULT_RANGE;

// Bumped on every redrawCharts() call and captured at entry. A stale
// invocation checks this after each await and abandons rather than
// appending into a grid a newer call has already started clearing --
// otherwise two quick range clicks (or a click racing the refresh button,
// or a theme change) interleave cards from two different time windows with
// no error anywhere.
let generation = 0;

// The zone signature (see thermalZoneSignature() below) the chart grid was
// actually drawn from, last time redrawCharts() ran. null until the first
// redraw.
let lastDrawnZoneSignature = null;

// Metric key -> [reader over state.probes, formatter]. The reader answers
// null for "not available" (probe missing, or its own status is not "ok"),
// which is rendered as an em dash rather than a fabricated zero.
export const TILES = [
  ["cpu.usage",
    (p) => (p.cpu?.status === "ok" ? p.cpu.usage_pct : null),
    (v) => `${formatNumber(v)} %`],
  ["cpu.temp.pkg",
    (p) => (p.thermal?.status === "ok" ? p.thermal.package_c : null),
    (v) => `${formatNumber(v)} °C`],
  ["mem.available",
    (p) => (p.memory?.status === "ok" ? p.memory.available : null),
    (v) => formatBytes(v)],
  ["mem.swap.used",
    (p) => (p.memory?.status === "ok" ? p.memory.swap_used : null),
    (v) => formatBytes(v)],
  ["battery.charge_pct",
    (p) => (p.battery?.status === "ok" ? p.battery.charge_pct : null),
    (v) => `${formatNumber(v)} %`],
  ["load.1",
    (p) => (p.cpu?.status === "ok" ? p.cpu.load1 : null),
    (v) => formatNumber(v)],
];

// Requested window length in days, keyed the same as history.js's RANGES.
const RANGE_DAYS = { "1h": 1 / 24, "24h": 1, "7d": 7, "90d": 90 };

// depth_days can land a hair under the requested window from rounding alone
// (the server rounds to two decimals) even when history genuinely covers
// it; the epsilon absorbs that without hiding a real shortfall. Kept at
// half the server's own rounding step (0.01 day): anything larger than
// that risks swallowing a real shortfall on the 1h range, whose entire
// window (1/24 ~= 0.0417 day) is barely four times the old 0.05 epsilon --
// with that epsilon, depthDays + 0.05 was never less than 0.0417 for any
// depthDays >= 0, so ui.chart.depth_short was unreachable on that range.
export function isDepthShort(depthDays, rangeId) {
  const requested = RANGE_DAYS[rangeId];
  return requested != null && depthDays + 0.005 < requested;
}

// The zones a probe sample that read "ok" carries -- {hwmon name: celsius}
// -- or {} for a machine with none (or a thermal probe that is not "ok").
// Shared by chartCards() (below) and thermalZoneSignature() (further
// down), which redrawCharts()'s caller uses to notice when the set changes
// after the charts were last drawn.
function thermalZones(state) {
  return state?.probes?.thermal?.status === "ok"
    ? state.probes.thermal.zones || {}
    : {};
}

// Every card except thermal is a fixed list of metric keys. The thermal
// card also draws every zone the kernel exposed on this machine
// (thermal.acpitz, thermal.x86_pkg_temp, ...) -- see
// healthconsole/probes/thermal.py: those names are discovered from
// /sys/class/hwmon at runtime, differ per machine, and so cannot be a
// static list. When the machine reports no zones (or the probe itself is
// unavailable), the card still draws cpu.temp.pkg alone -- a deliberate,
// not a broken, picture.
export function chartCards(state) {
  const zones = thermalZones(state);
  const thermalMetrics = ["cpu.temp.pkg",
    ...Object.keys(zones).map((zone) => `thermal.${zone}`)];
  return [
    ["ui.chart.group.cpu", ["cpu.usage", "load.1"]],
    ["ui.chart.group.thermal", thermalMetrics],
    ["ui.chart.group.memory", ["mem.available_pct", "mem.swap.used"]],
    ["ui.chart.group.battery", ["battery.charge_pct", "battery.wear_pct"]],
  ];
}

// A cheap fingerprint of which zones the thermal card would draw for a
// given state -- sorted so the fingerprint depends only on the set, not on
// object key order. Compared, on every renderExpert() tick, against the
// zone set the chart grid was actually last drawn from (see
// lastDrawnZoneSignature below): if `/api/now` fails on load, the thermal
// card falls back to cpu.temp.pkg alone with no way to tell that apart
// from a machine that genuinely has no zones, and nothing would otherwise
// ever redraw it once the real zones become known from the first SSE
// state.
export function thermalZoneSignature(state) {
  return Object.keys(thermalZones(state)).sort().join(",");
}

// Colour is the only channel separating series within one chart. Cycling a
// small fixed palette by dataset index (rather than tying it to which card
// this is) keeps every series in a multi-metric card visually distinct --
// two series sharing one card no longer land on the same default colour --
// and a dash pattern beyond the palette's length keeps a fifth+ series (a
// machine with several thermal zones) from repeating a colour
// indistinguishably from an earlier one.
const COLOUR_PALETTE = ["--hc-info", "--hc-ok", "--hc-attention", "--hc-urgent"];
const DASH_PATTERNS = [[], [6, 3], [2, 2], [8, 3, 2, 3]];

function seriesStyle(index) {
  const cycle = Math.floor(index / COLOUR_PALETTE.length);
  return {
    borderColor: themeColour(COLOUR_PALETTE[index % COLOUR_PALETTE.length]),
    borderDash: DASH_PATTERNS[cycle % DASH_PATTERNS.length],
  };
}

export function currentRange() { return range; }

function paintRangeButtons() {
  const group = el("range-group");
  for (const button of group.querySelectorAll("button")) {
    const active = button.dataset.range === range;
    button.setAttribute("aria-pressed", String(active));
    button.classList.toggle("active", active);
  }
}

// Builds the range buttons (translated, so this must run after the
// catalogue is ready) and wires each one to setRange(). Safe to call again
// on a locale change: it rebuilds the buttons from scratch and restores
// which one is pressed from the module's own `range` state.
export function paintRangeControl() {
  const group = el("range-group");
  clear(group);
  for (const id of RANGES) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-outline-secondary";
    button.textContent = translate(`ui.range.${id}`);
    button.dataset.range = id;
    button.addEventListener("click", () => { setRange(id); });
    group.append(button);
  }
  paintRangeButtons();
}

export async function setRange(next) {
  if (!RANGES.includes(next)) return;
  range = next;
  paintRangeButtons();
  await redrawCharts();
}

function renderTiles(state) {
  const row = el("expert-tiles");
  clear(row);
  const heading = document.createElement("h2");
  heading.className = "visually-hidden";
  heading.textContent = translate("ui.expert.overview");
  row.append(heading);
  const probes = (state && state.probes) || {};
  for (const [metric, read, format] of TILES) {
    const value = read(probes);
    const column = document.createElement("div");
    column.className = "col-6 col-md-4 col-lg-2";
    const card = document.createElement("div");
    card.className = "card h-100 text-center";
    const body = document.createElement("div");
    body.className = "card-body p-2";
    const label = document.createElement("p");
    label.className = "small text-body-secondary mb-1";
    label.textContent = metricLabel(metric);
    const reading = document.createElement("p");
    reading.className = "h5 mb-0";
    reading.textContent = value == null ? "—" : format(value);
    body.append(label, reading);
    card.append(body);
    column.append(card);
    row.append(column);
  }
}

export function renderProbeTable(state) {
  const table = el("probe-table");
  clear(table);
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const key of ["ui.expert.probe_table.name", "ui.expert.probe_table.status",
                     "ui.expert.probe_table.detail"]) {
    const headCell = document.createElement("th");
    headCell.scope = "col";
    headCell.textContent = translate(key);
    headRow.append(headCell);
  }
  head.append(headRow);
  table.append(head);
  const body = document.createElement("tbody");
  for (const [name, probe] of Object.entries((state && state.probes) || {})) {
    const row = document.createElement("tr");
    const nameCell = document.createElement("th");
    nameCell.scope = "row";
    nameCell.textContent = name;
    const statusCell = document.createElement("td");
    // Left untranslated deliberately: "ok" / "unavailable" / "incoherent"
    // are diagnostic material, the same ruling already applied to the raw
    // reason column beside it and to the untranslated probe reason simple.js
    // shows behind a translated lead-in.
    statusCell.textContent = probe.status || "";
    const detailCell = document.createElement("td");
    // Diagnostic material only -- reason/eval_error are raw English from
    // the probe itself (healthconsole/probes/__init__.py's unavailable()),
    // never user-facing prose, exactly like the raw-detail line simple.js
    // shows beside an unavailable finding.
    detailCell.textContent = probe.reason || probe.eval_error || "";
    row.append(nameCell, statusCell, detailCell);
    body.append(row);
  }
  table.append(body);
}

function notice(variant, text) {
  const div = document.createElement("div");
  div.className = `alert ${variant} mb-0`;
  div.textContent = text;
  return div;
}

function chartCard(title) {
  const column = document.createElement("div");
  column.className = "col-12 col-lg-6";
  const card = document.createElement("div");
  card.className = "card h-100";
  const body = document.createElement("div");
  body.className = "card-body";
  const heading = document.createElement("h2");
  heading.className = "h6";
  heading.textContent = title;
  body.append(heading);
  card.append(body);
  column.append(card);
  return { column, body };
}

// depth_days comes back from every /api/history response and, until this
// task, was ignored on this side. Without this line, a 90 d window that
// only holds six days of history reads as a collection failure rather than
// as a history that has simply just begun.
//
// Below one day, {days} rounds to "0" under formatNumber's one-decimal
// display (a console running twenty minutes is depth_days: 0.01) and reads
// as a collection failure of its own -- the same falsehood this feature
// exists to prevent, just with fewer digits. Below that threshold the
// value is shown in hours instead, through a second pair of catalogue
// keys.
const HOURS_BELOW_DAYS = 1;

// formatNumber() rounds -- not floors -- to one decimal for display. That
// let the short sentence's own number round up to exactly the figure the
// window itself asks for: a depth of 6.97 on the 7d range is genuinely
// short (isDepthShort() compares at full precision, unaffected by any of
// this), but displayed after formatNumber's rounding it read "Only 7 days
// of history so far" -- naming the full window as if it were the
// shortfall. Flooring only the number shown inside the short sentence
// keeps it from ever reaching the window's own count, without touching
// isDepthShort's own, separately-tuned precision.
function flooredToOneDecimal(value) {
  return Math.floor(value * 10) / 10;
}

// Pure: decides which catalogue key describes a card's depth and what
// number to interpolate into it. Exported, and depthLine() kept as a thin
// DOM wrapper around it, so the decision -- not just the catalogue key
// names it happens to use -- can be exercised directly, the same way
// isDepthShort() already is.
export function depthMessage(depthDays, rangeId) {
  const short = isDepthShort(depthDays, rangeId);
  const useHours = depthDays < HOURS_BELOW_DAYS;
  const key = useHours
    ? (short ? "ui.chart.depth_short_hours" : "ui.chart.depth_hours")
    : (short ? "ui.chart.depth_short" : "ui.chart.depth");
  const raw = useHours ? depthDays * 24 : depthDays;
  const value = short ? flooredToOneDecimal(raw) : raw;
  return { key, params: useHours ? { hours: value } : { days: value } };
}

function depthLine(depthDays) {
  const paragraph = document.createElement("p");
  paragraph.className = "small text-body-secondary mt-2 mb-0";
  const { key, params } = depthMessage(depthDays, range);
  paragraph.textContent = translate(key, params);
  return paragraph;
}

export async function redrawCharts() {
  const myGeneration = ++generation;
  const grid = el("chart-grid");
  // Chart.js keeps a live instance per canvas; clearing the grid first
  // would detach the canvases without releasing them. destroyIn() must run
  // before the grid is cleared, not after.
  destroyIn(grid);
  clear(grid);
  // Whatever the thermal card ends up drawing below is drawn from this
  // state's zones; remember which, so renderExpert() can notice later if a
  // newer state carries a different set.
  lastDrawnZoneSignature = thermalZoneSignature(lastKnownState());

  if (!chartsAvailable()) {
    grid.append(notice("alert-warning", translate("ui.chart.unavailable")));
    return;
  }

  for (const [titleKey, metrics] of chartCards(lastKnownState())) {
    if (myGeneration !== generation) return;
    const card = chartCard(translate(titleKey));
    grid.append(card.column);

    let series;
    try {
      series = await Promise.all(metrics.map((metric) => fetchSeries(metric, range)));
    } catch (error) {
      if (myGeneration !== generation) { card.column.remove(); return; }
      console.warn("history unavailable", error);
      card.body.append(notice("alert-danger", translate("ui.error.history")));
      continue;
    }
    if (myGeneration !== generation) { card.column.remove(); return; }

    // Each metric is kept paired with its own series through the empty
    // filter, so a dropped (empty) series takes its label with it -- never
    // the first metric's name stitched onto the first non-empty series'
    // numbers, which is what a plain series.filter() alone (with metrics
    // indexed separately) would risk the moment series[0] is the empty one.
    const drawable = metrics
      .map((metric, index) => ({ metric, series: series[index] }))
      .filter((pair) => pair.series.points.length > 0);
    if (drawable.length === 0) {
      // Not a flat line at zero: a flat line is a measurement, and the
      // absence of one is not.
      card.body.append(notice("alert-light", translate("ui.chart.empty")));
      continue;
    }

    const canvas = document.createElement("canvas");
    const holder = document.createElement("div");
    holder.className = "chart-holder";
    holder.append(canvas);
    card.body.append(holder);
    drawLine(canvas, {
      range,
      points: drawable[0].series.points,
      // The server returns points already ascending by timestamp, which is
      // exactly the precondition normalized: true promises (see charts.js).
      datasets: drawable.map(({ metric, series: s }, index) => ({
        label: metricLabel(metric),
        data: s.points.map(([x, y]) => ({ x, y })),
        normalized: true,
        borderWidth: 2, fill: false, tension: 0.25, pointRadius: 0,
        ...seriesStyle(index),
      })),
    });
    // A <canvas> has no other accessible content: this sentence is all an
    // assistive-technology user gets, so it must describe every series
    // actually drawn -- not just the first metric named in the card,
    // which can be the one series that got filtered out above.
    labelChart(canvas, drawable
      .map(({ metric, series: s }) => describeSeries(metric, range, s.points))
      .join(" "));
    // Only the drawn series count towards "how much history is there": an
    // empty series (already excluded from `drawable` above) always reports
    // depth_days: 0 from the server, and folding that into the minimum
    // would print "0 days" beneath a chart that is, in fact, full.
    card.body.append(
      depthLine(Math.min(...drawable.map(({ series: s }) => s.depthDays))));
  }
}

export function renderExpert(state) {
  renderTiles(state);
  renderProbeTable(state);
  // The only owner of #raw. simple.js used to write it too, but that
  // element lives inside the Expert accordion and can only be seen while
  // Expert mode is showing -- which is exactly when renderExpert runs, on
  // every SSE event, whatever the state.
  el("raw").textContent = JSON.stringify(state, null, 2);
  // The one deliberate exception to "redrawCharts() never runs from the 2s
  // tick": if the zones this state reports differ from the set the chart
  // grid was actually drawn from, redraw once to pick them up. A cheap
  // string comparison (the same discipline findingsSignature()/
  // gaugeSignature() already apply in simple.js) keeps this from doing
  // anything at all on the far more common tick where the zone set has not
  // changed.
  if (thermalZoneSignature(state) !== lastDrawnZoneSignature) {
    redrawCharts();
  }
}
