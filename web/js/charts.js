// Chart.js wrapper. Everything that knows about canvases lives here, so the
// rest of the front end keeps working when the library does not load.

import { translate, formatNumber, formatBytes, formatTime, formatDate } from "./i18n.js";

// Above this, a raw draw saturates the canvas and stutters on a phone: 90
// days of 5-minute aggregates is ~26,000 points, and even 24 h from the raw
// table at a 30 s step is ~2,900. Tied to the point count rather than to
// which table the server chose, so both cases are covered.
const DECIMATION_THRESHOLD = 1000;

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
// differ per machine, so no catalogue can cover them; showing the raw key is
// honest, an empty label is not. Skipping translate() for them (rather than
// letting it miss and warn) also spares five to eight missing-key warnings
// per redraw on a typical laptop, drowning out a warning meant to flag real
// catalogue bugs.
export function metricLabel(metricKey) {
  if (metricKey.startsWith("thermal.")) return metricKey;
  const network = /^net\.(.+)\.(rx|tx)_bps$/.exec(metricKey);
  if (network) {
    return translate(`ui.metric.net.${network[2]}_bps`, { iface: network[1] });
  }
  return translate(`ui.metric.${metricKey}`) || metricKey;
}

// A metric's display unit -- drives the accessible summary sentence and the
// y-axis tick labels (describeSeries()/drawLine() below), and which series
// share an axis. Every hwmon zone matches cpu.temp.pkg's unit, hence the
// thermal.* prefix match rather than one entry per zone.
const METRIC_UNITS = {
  "mem.available": "bytes",
  "mem.swap.used": "bytes",
  "mem.available_pct": "pct",
  "cpu.usage": "pct",
  "battery.charge_pct": "pct",
  "battery.wear_pct": "pct",
  "cpu.temp.pkg": "celsius",
};

export function metricUnit(metricKey) {
  if (metricKey.startsWith("thermal.")) return "celsius";
  if (metricKey.startsWith("net.") && metricKey.endsWith("_bps")) return "bps";
  return METRIC_UNITS[metricKey] || "";
}

function formatByUnit(value, unit) {
  if (unit === "bytes") return formatBytes(value);
  if (unit === "bps") return `${formatBytes(value)}/s`;
  if (unit === "pct") return `${formatNumber(value)} %`;
  if (unit === "celsius") return `${formatNumber(value)} °C`;
  return formatNumber(value);
}

export function describeSeries(metricKey, range, points) {
  const summary = summarise(points);
  if (!summary) return translate("ui.chart.empty");
  const unit = metricUnit(metricKey);
  return translate("ui.chart.summary", {
    metric: metricLabel(metricKey),
    range: translate(`ui.range.${range}`),
    min: formatByUnit(summary.min, unit),
    max: formatByUnit(summary.max, unit),
    current: formatByUnit(summary.current, unit),
  });
}

// Chart.js keeps a live instance per canvas; detaching the canvas from the
// document does not release it. renderSimple() repaints on every SSE event
// (every 2s), so failing to destroy first would leak an instance per
// finding, per tick, for as long as the page stays open. Called on a
// container (e.g. #findings, or the gauge's holder) rather than a single
// canvas, so one call clears every sparkline a repaint is about to replace.
export function destroyIn(container) {
  if (!chartsAvailable()) return;
  for (const canvas of container.querySelectorAll("canvas")) {
    const chart = globalThis.Chart.getChart(canvas);
    if (chart) chart.destroy();
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
// never reads options.normalized. Callers must set it on each dataset
// object they pass in, e.g. { data, normalized: true, ... }; sound here
// because the server returns points already ascending by timestamp, exactly
// normalized: true's precondition.
//
// Each dataset also carries a `metric` field (not a Chart.js option --
// Chart.js ignores it). A card can pair metrics that disagree in unit (a
// percentage and a raw byte count, e.g. the Memory card's mem.available_pct
// and mem.swap.used): one shared linear scale flattens whichever series has
// the smaller range to an uninformative line at zero. A dataset whose unit
// differs from the first dataset's gets its own right-hand axis instead --
// decided from the metric (metricUnit(), above), not the card.
export function drawLine(canvas, { datasets, range, points }) {
  if (!chartsAvailable()) return null;
  const options = baseOptions(points);
  options.plugins.legend = { display: datasets.length > 1 };
  const primaryUnit = datasets.length > 0 ? metricUnit(datasets[0].metric) : "";
  for (const dataset of datasets) {
    if (metricUnit(dataset.metric) !== primaryUnit) dataset.yAxisID = "y2";
  }
  const secondDataset = datasets.find((dataset) => dataset.yAxisID === "y2");
  options.scales = {
    x: {
      type: "linear",
      ticks: { callback: (value) => formatTick(value, range), maxTicksLimit: 6 },
      grid: { color: themeColour("--hc-grid") },
    },
    y: {
      grid: { color: themeColour("--hc-grid") }, beginAtZero: false,
      ticks: { callback: (value) => formatByUnit(value, primaryUnit) },
    },
  };
  if (secondDataset) {
    options.scales.y2 = {
      position: "right", beginAtZero: false,
      grid: { drawOnChartArea: false },
      ticks: { callback: (value) => formatByUnit(value, metricUnit(secondDataset.metric)) },
    };
  }
  return new globalThis.Chart(canvas, { type: "line", data: { datasets }, options });
}

export function drawSparkline(canvas, points, colour) {
  if (!chartsAvailable()) return null;
  const options = baseOptions(points);
  options.plugins.tooltip = { enabled: false };
  options.scales = { x: { display: false, type: "linear" },
                     y: { display: false } };
  options.elements = { point: { radius: 0 } };
  return new globalThis.Chart(canvas, {
    type: "line",
    data: { datasets: [{ data: points.map(([x, y]) => ({ x, y })),
                         normalized: true,
                         borderColor: colour, borderWidth: 2,
                         fill: false, tension: 0.3 }] },
    options,
  });
}

// Puts the score inside the ring, per the approved mockup. #score (read by
// screen readers -- the canvas is aria-hidden) stays put; this is purely
// the sighted, in-canvas echo of the same number. A plugin rather than an
// overlaid DOM node, since Chart.js owns the canvas's box and a node placed
// over it would have to track the ring's size by hand.
const gaugeCenterText = {
  id: "gaugeCenterText",
  // Chart.js hands a plugin its own options (chart.options.plugins.<id>)
  // back as this third argument.
  afterDraw(chart, _args, pluginOptions) {
    if (!pluginOptions || !pluginOptions.text) return;
    const { ctx, chartArea: { left, right, top, bottom } } = chart;
    ctx.save();
    ctx.font = "700 28px system-ui, sans-serif";
    ctx.fillStyle = pluginOptions.colour;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(pluginOptions.text, (left + right) / 2, (top + bottom) / 2);
    ctx.restore();
  },
};

export function drawGauge(canvas, score, colour) {
  if (!chartsAvailable()) return null;
  return new globalThis.Chart(canvas, {
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
      plugins: {
        legend: { display: false }, tooltip: { enabled: false },
        gaugeCenterText: { text: String(score), colour: themeColour("--bs-body-color") },
      },
    },
    plugins: [gaugeCenterText],
  });
}

// A <canvas> is invisible to a screen reader, so every chart carries a
// sentence saying what it shows.
export function labelChart(canvas, text) {
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", text);
}
