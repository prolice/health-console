// Simple mode rendering. No wording in the code -- every string comes from
// the active catalogue via translate(). It does reach outside the page for
// one thing: a per-finding sparkline fetched through history.js, and that
// fetch is illustration only -- its failure never blocks or blemishes a
// verdict that is otherwise perfectly valid.

import { el, setText } from "./dom.js";
import { currentLocale, translate, formatFindingParams } from "./i18n.js";
import { hasMeasurement } from "./stream.js";
import { chartsAvailable, describeSeries, destroyIn, drawGauge,
         drawSparkline, labelChart, themeColour } from "./charts.js";
import { fetchSeries } from "./history.js";

// Which recorded metric illustrates which finding. battery.incoherent is
// deliberately absent: the console has just declared those readings
// untrustworthy, and drawing a curve from them anyway would take that claim
// back in the same breath. A test asserts the mapping stays this way.
export const FINDING_METRIC = {
  "cpu.usage_high": "cpu.usage",
  "thermal.high": "cpu.temp.pkg",
  "thermal.critical": "cpu.temp.pkg",
  "memory.pressure": "mem.available_pct",
  "battery.wear": "battery.wear_pct",
};

const SEVERITY_COLOUR = {
  OK: "--hc-ok", INFO: "--hc-info",
  ATTENTION: "--hc-attention", URGENT: "--hc-urgent",
};

let lastFindingsSignature = null;

function card(severity, titleText, whyText) {
  const article = document.createElement("article");
  // Bootstrap's .card supplies the background, the border style and the
  // colour base; .finding only narrows the left edge (style.css) to a
  // 4px severity stripe. Without .card, border-left-width has nothing to
  // paint against -- Bootstrap's default border-style is none.
  article.className = `finding card severity-${severity}`;
  const body = document.createElement("div");
  body.className = "card-body";
  const tag = document.createElement("p");
  tag.className = "tag";
  // Colour never carries the state alone: icon and word travel together.
  tag.textContent = `${translate(`severity.${severity}.icon`)} `
    + translate(`severity.${severity}.word`);
  const heading = document.createElement("h2");
  heading.textContent = titleText;
  const why = document.createElement("p");
  why.textContent = whyText;
  body.append(tag, heading, why);
  article.append(body);
  return article;
}

// The sparkline is illustration, not measurement: if the history call
// fails, the card keeps its verdict and simply shows no curve. Never an
// error banner over a finding that is itself perfectly valid.
async function attachSparkline(article, findingId, severity) {
  const metric = FINDING_METRIC[findingId];
  if (!metric || !chartsAvailable()) return;
  let series;
  try {
    series = await fetchSeries(metric, "1h");
  } catch (error) {
    console.warn(`sparkline unavailable for ${findingId}`, error);
    return;
  }
  if (series.points.length === 0) return;
  // A later, different repaint can tear this card down while the fetch
  // above was still in flight. Drawing into a detached article would
  // create a Chart.js instance destroyIn() can never reach again, since it
  // no longer lives inside #findings -- a slower, rarer version of the
  // same leak this task exists to close.
  if (!article.isConnected) return;
  const holder = document.createElement("div");
  holder.className = "sparkline-holder";
  const canvas = document.createElement("canvas");
  holder.append(canvas);
  article.querySelector(".card-body").append(holder);
  labelChart(canvas, describeSeries(metric, "1h", series.points));
  drawSparkline(canvas, series.points,
                themeColour(SEVERITY_COLOUR[severity] || "--hc-info"));
}

// A cheap fingerprint of what #findings currently displays. renderSimple
// runs on every SSE event (every 2s); comparing this against the last one
// drawn is what lets it skip tearing down and rebuilding -- and
// re-animating -- every card and sparkline when the picture has not
// actually changed. Locale is part of the fingerprint too: switching
// language changes every word on the cards without changing a single
// finding or probe.
export function findingsSignature(state) {
  const findings = (state.findings || [])
    .map((finding) => [finding.id, finding.severity, finding.params]);
  const probes = Object.entries(state.probes || {})
    .filter(([, probe]) => !(probe.status === "ok" && !probe.eval_error))
    .map(([name, probe]) => [name, probe.reason, probe.eval_error]);
  return JSON.stringify({ locale: currentLocale(), findings, probes });
}

function rebuildFindings(state) {
  const host = el("findings");
  destroyIn(host);
  host.textContent = "";
  for (const finding of state.findings || []) {
    const params = formatFindingParams(finding.params);
    const article = card(
      finding.severity,
      translate(`finding.${finding.id}.title`, params),
      translate(`finding.${finding.id}.why`, params));
    host.append(article);
    attachSparkline(article, finding.id, finding.severity);
  }

  // An unavailable probe is shown as unavailable, never as a reassuring zero.
  // A probe whose evaluate() raised keeps status "ok" (the reading itself
  // succeeded) but carries eval_error, and that must not stay invisible.
  for (const [name, probe] of Object.entries(state.probes || {})) {
    if (probe.status === "ok" && !probe.eval_error) continue;
    const article = card("INFO",
      translate("ui.probe.unavailable", { probe: name }),
      translate("ui.probe.unavailable.why"));
    // probe.reason/eval_error is diagnostic material in English -- see
    // healthconsole/probes/__init__.py's unavailable() -- never translated
    // prose. It stays visible (hiding it would be its own dishonesty) but
    // is rendered as a visually secondary line behind a translated lead-in,
    // rather than as the card's whole explanation.
    const raw = probe.reason || probe.eval_error || "";
    if (raw) {
      const detail = document.createElement("p");
      detail.className = "raw-detail";
      detail.textContent =
        `${translate("ui.probe.unavailable.raw_prefix")} ${raw}`;
      article.querySelector(".card-body").append(detail);
    }
    host.append(article);
  }
}

function renderNoMeasurement(state) {
  el("verdict-icon").textContent = "";
  setText(el("verdict-word"), translate("ui.state.no_measurement"));
  setText(el("verdict-sentence"), translate("ui.state.no_measurement.detail"));
  setText(el("score"), "—");
  destroyIn(el("verdict-gauge").parentElement);
  const host = el("findings");
  destroyIn(host);
  host.textContent = "";
  lastFindingsSignature = null;
  el("raw").textContent = JSON.stringify(state, null, 2);
}

export function renderSimple(state) {
  if (!hasMeasurement(state)) {
    renderNoMeasurement(state);
    return;
  }

  const severity = state.severity || "OK";
  el("verdict-icon").textContent = translate(`severity.${severity}.icon`);
  setText(el("verdict-word"), translate(`severity.${severity}.word`));
  setText(el("verdict-sentence"), translate(`verdict.${severity}`));
  setText(el("score"), String(state.score));

  const gaugeCanvas = el("verdict-gauge");
  destroyIn(gaugeCanvas.parentElement);
  drawGauge(gaugeCanvas, state.score,
            themeColour(SEVERITY_COLOUR[severity] || "--hc-info"));

  // See findingsSignature(): rebuilding the cards (and re-fetching every
  // sparkline) is skipped whenever what they display has not changed since
  // the last tick.
  const signature = findingsSignature(state);
  if (signature !== lastFindingsSignature) {
    lastFindingsSignature = signature;
    rebuildFindings(state);
  }

  el("raw").textContent = JSON.stringify(state, null, 2);
}
