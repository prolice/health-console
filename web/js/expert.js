// The dense view: current readings, then history over one window shared by
// every chart, then the material of last resort.
//
// The tile row deliberately carries no ARIA "live region" marker: it
// repaints on every SSE event (every 2s), and one there would be a screen
// reader talking without pause. redrawCharts() is the one thing in this
// module that must NOT run from that 2s tick -- it only runs on a range
// change, the refresh button, a theme change, and on first entering Expert
// mode.

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
// it; the epsilon absorbs that without hiding a real shortfall.
export function isDepthShort(depthDays, rangeId) {
  const requested = RANGE_DAYS[rangeId];
  return requested != null && depthDays + 0.05 < requested;
}

// Every card except thermal is a fixed list of metric keys. The thermal
// card also draws every zone the kernel exposed on this machine
// (thermal.acpitz, thermal.x86_pkg_temp, ...) -- see
// healthconsole/probes/thermal.py: a probe sample that read "ok" carries
// zones as {hwmon name: celsius}, and those names are discovered from
// /sys/class/hwmon at runtime, differ per machine, and so cannot be a
// static list. When the machine reports no zones (or the probe itself is
// unavailable), the card still draws cpu.temp.pkg alone -- a deliberate,
// not a broken, picture.
export function chartCards(state) {
  const zones = state?.probes?.thermal?.status === "ok"
    ? state.probes.thermal.zones || {}
    : {};
  const thermalMetrics = ["cpu.temp.pkg",
    ...Object.keys(zones).map((zone) => `thermal.${zone}`)];
  return [
    ["ui.chart.group.cpu", ["cpu.usage", "load.1"], "--hc-info"],
    ["ui.chart.group.thermal", thermalMetrics, "--hc-urgent"],
    ["ui.chart.group.memory", ["mem.available_pct", "mem.swap.used"], "--hc-ok"],
    ["ui.chart.group.battery", ["battery.charge_pct", "battery.wear_pct"], "--hc-attention"],
  ];
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

function renderProbeTable(state) {
  const table = el("probe-table");
  clear(table);
  const body = document.createElement("tbody");
  for (const [name, probe] of Object.entries((state && state.probes) || {})) {
    const row = document.createElement("tr");
    const nameCell = document.createElement("th");
    nameCell.scope = "row";
    nameCell.textContent = name;
    const statusCell = document.createElement("td");
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
function depthLine(depthDays) {
  const paragraph = document.createElement("p");
  paragraph.className = "small text-body-secondary mt-2 mb-0";
  paragraph.textContent = translate(
    isDepthShort(depthDays, range) ? "ui.chart.depth_short" : "ui.chart.depth",
    { days: depthDays });
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

  if (!chartsAvailable()) {
    grid.append(notice("alert-warning", translate("ui.chart.unavailable")));
    return;
  }

  for (const [titleKey, metrics, colourVar] of chartCards(lastKnownState())) {
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

    const drawable = series.filter((s) => s.points.length > 0);
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
      points: drawable[0].points,
      // The server returns points already ascending by timestamp, which is
      // exactly the precondition normalized: true promises (see charts.js).
      datasets: metrics.map((metric, index) => ({
        label: metricLabel(metric),
        data: series[index].points.map(([x, y]) => ({ x, y })),
        normalized: true,
        borderColor: themeColour(index === 0 ? colourVar : "--hc-info"),
        borderWidth: 2, fill: false, tension: 0.25, pointRadius: 0,
      })),
    });
    labelChart(canvas, describeSeries(metrics[0], range, drawable[0].points));
    card.body.append(depthLine(Math.min(...series.map((s) => s.depthDays))));
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
}
