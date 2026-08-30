// Chart.js wrapper. Everything that knows about canvases lives here, so the
// rest of the front end keeps working when the library does not load.

import { translate, formatNumber, formatTime, formatDate } from "./i18n.js";

// Above this, a raw draw saturates the canvas and stutters on a phone: 90
// days of 5-minute aggregates is ~26,000 points, and even 24 h from the raw
// table at a 30 s step is ~2,900. Tied to the point count rather than to
// which table the server chose, so both cases are covered.
const DECIMATION_THRESHOLD = 1000;

const live = new Set();

export function chartsAvailable() {
  return typeof globalThis.Chart === "function";
}

export function summarise(points) {
  if (!points || points.length === 0) return null;
  const values = points
    .map(([, value]) => value)
    .filter((value) => typeof value === "number" && Number.isFinite(value));
  if (values.length === 0) return null;
  // current is the most recent *finite* sample, not necessarily the sample
  // from the newest bucket: a trailing null (a collector hiccup, or simply
  // the freshest bucket not yet populated) is skipped rather than reported
  // as a live reading. That is why the catalogue wording for ui.chart.summary
  // says "last recorded {current}" rather than "currently {current}" -- the
  // value may be stale by up to one sampling gap, and the sentence must not
  // claim otherwise.
  return {
    min: Math.min(...values),
    max: Math.max(...values),
    current: values[values.length - 1],
  };
}

export function shouldDecimate(points) {
  return Boolean(points) && points.length > DECIMATION_THRESHOLD;
}

export function themeColour(name) {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
}

function animationsAllowed() {
  return !globalThis.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// Zone keys like thermal.acpitz are discovered from the kernel at runtime and
// differ per machine, so no catalogue can cover them. The design document
// already classes raw zone names as untranslated diagnostic material; showing
// one is honest, showing an empty label is not.
export function metricLabel(metricKey) {
  return translate(`ui.metric.${metricKey}`) || metricKey;
}

export function describeSeries(metricKey, range, points) {
  const summary = summarise(points);
  if (!summary) return translate("ui.chart.empty");
  return translate("ui.chart.summary", {
    metric: metricLabel(metricKey),
    range: translate(`ui.range.${range}`),
    min: formatNumber(summary.min),
    max: formatNumber(summary.max),
    current: formatNumber(summary.current),
  });
}

function register(chart) { live.add(chart); return chart; }

export function destroyAll() {
  for (const chart of live) chart.destroy();
  live.clear();
}

// Chart.js keeps a live instance per canvas; detaching the canvas from the
// document does not release it. renderSimple() repaints on every SSE event
// (every 2s), so failing to destroy first would leak an instance per
// finding, per tick, for as long as the page stays open. Called on a
// container (e.g. #findings, or the gauge's card-body) rather than a single
// canvas, so one call clears every sparkline a repaint is about to replace.
export function destroyIn(container) {
  if (!chartsAvailable()) return;
  for (const canvas of container.querySelectorAll("canvas")) {
    const chart = globalThis.Chart.getChart(canvas);
    if (chart) {
      chart.destroy();
      live.delete(chart);
    }
  }
}

function baseOptions(points) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: animationsAllowed() && { duration: 200 },
    parsing: false,
    plugins: {
      legend: { display: false },
      decimation: {
        enabled: shouldDecimate(points),
        algorithm: "lttb",
        samples: 500,
      },
    },
  };
}

// No date adapter: the tick labels are formatted through the same
// Intl.DateTimeFormat the rest of the page uses, so chart dates follow
// the active locale without a second formatting system to keep in sync.
function formatTick(unixSeconds, range) {
  const when = new Date(unixSeconds * 1000);
  return (range === "1h" || range === "24h")
    ? formatTime(when) : formatDate(when);
}

// normalized is a per-dataset flag, not a chart-options one -- Chart.js
// never reads options.normalized. Callers (Task 9) must set it on each
// dataset object they pass in, e.g. { data, normalized: true, ... }. It is
// sound here because the server returns points already ascending by
// timestamp, which is exactly the precondition normalized: true promises.
export function drawLine(canvas, { datasets, range, points }) {
  if (!chartsAvailable()) return null;
  const options = baseOptions(points);
  options.plugins.legend = { display: datasets.length > 1 };
  options.scales = {
    x: {
      type: "linear",
      ticks: { callback: (value) => formatTick(value, range), maxTicksLimit: 6 },
      grid: { color: themeColour("--hc-grid") },
    },
    y: { grid: { color: themeColour("--hc-grid") }, beginAtZero: false },
  };
  return register(new globalThis.Chart(canvas, {
    type: "line", data: { datasets }, options,
  }));
}

export function drawSparkline(canvas, points, colour) {
  if (!chartsAvailable()) return null;
  const options = baseOptions(points);
  options.plugins.tooltip = { enabled: false };
  options.scales = { x: { display: false, type: "linear" },
                     y: { display: false } };
  options.elements = { point: { radius: 0 } };
  return register(new globalThis.Chart(canvas, {
    type: "line",
    data: { datasets: [{ data: points.map(([x, y]) => ({ x, y })),
                         normalized: true,
                         borderColor: colour, borderWidth: 2,
                         fill: false, tension: 0.3 }] },
    options,
  }));
}

export function drawGauge(canvas, score, colour) {
  if (!chartsAvailable()) return null;
  return register(new globalThis.Chart(canvas, {
    type: "doughnut",
    data: {
      datasets: [{
        data: [score, 100 - score],
        backgroundColor: [colour, themeColour("--hc-grid")],
        borderWidth: 0,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: true, cutout: "72%",
      animation: animationsAllowed() && { duration: 300 },
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
    },
  }));
}

// A <canvas> is invisible to a screen reader, so every chart carries a
// sentence saying what it shows.
export function labelChart(canvas, text) {
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", text);
}
