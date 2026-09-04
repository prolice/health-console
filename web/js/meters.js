// Capacity meters: the one place that knows a reading has a ceiling.
//
// "3.4 GB available" and "0 B of swap in use" are half sentences -- out of
// what, and is 0 B an idle swap file or no swap file at all? Each meter
// pairs a value with the capacity it is measured against, and both views
// render that pair the same way.
//
// Colour is borrowed, never invented: a meter tints itself only when the
// server's rules engine has opened a finding naming it (see meterSeverity),
// so a bar can never contradict the verdict above it. Hence battery.charge_pct
// declares no findings -- findings.py has no low-charge finding to borrow.

import { clear, el, setText } from "./dom.js";
import { formatBytes, formatNumber, translate } from "./i18n.js";
import { metricLabel } from "./charts.js";

// probe: where to read. fields: [value, capacity]; a null capacity means the
// value is already a percentage. findings: ids that may tint it. strip:
// whether Simple mode shows it. id doubles as the metric key metricLabel()
// translates.
export const METERS = [
  { id: "mem.available", probe: "memory", unit: "bytes",
    fields: ["available", "total"], findings: ["memory.pressure"], strip: true },
  { id: "mem.swap.used", probe: "memory", unit: "bytes",
    fields: ["swap_used", "swap_total"], findings: ["memory.pressure"], strip: true },
  { id: "disk.root.used_pct", probe: "storage", unit: "percent",
    fields: ["used_pct", null], findings: ["storage.root_full"], strip: true },
  { id: "battery.charge_pct", probe: "battery", unit: "percent",
    fields: ["charge_pct", null], findings: [], strip: true },
  // Expert only: it moves on every 2s tick, and a bar that never settles
  // defeats the at-a-glance reading the Simple strip exists for.
  { id: "cpu.usage", probe: "cpu", unit: "percent",
    fields: ["usage_pct", null], findings: ["cpu.usage_high"], strip: false },
];

export function stripMeters() {
  return METERS.filter((meter) => meter.strip);
}

// null means "no bar", which is not a bar at zero. A capacity of zero is a
// real reading (a machine with no swap file); what it lacks is a share.
export function meterFraction(value, capacity) {
  if (!Number.isFinite(value) || !Number.isFinite(capacity)) return null;
  if (capacity <= 0) return null;
  return Math.min(1, Math.max(0, value / capacity));
}

// null for "nothing trustworthy to show" -- probe missing, not "ok", or a
// field absent or non-finite. Never a fabricated zero: an unavailable probe
// is shown as unavailable everywhere else, and a meter must not be the one
// place it reads as an empty tank.
export function readMeter(meter, probes) {
  const probe = (probes || {})[meter.probe];
  if (!probe || probe.status !== "ok") return null;
  const [valueField, capacityField] = meter.fields;
  const value = probe[valueField];
  const capacity = capacityField === null ? 100 : probe[capacityField];
  if (!Number.isFinite(value) || !Number.isFinite(capacity)) return null;
  return { unit: meter.unit, value, capacity,
           fraction: meterFraction(value, capacity) };
}

const SEVERITY_RANK = { OK: 0, INFO: 1, ATTENTION: 2, URGENT: 3 };

// The worst open finding naming this meter, or null. Ranked, not first-wins:
// two findings can name one meter and the bar must show the worse.
export function meterSeverity(meter, findings) {
  let worst = null;
  for (const finding of findings || []) {
    if (!meter.findings.includes(finding.id)) continue;
    const severity = finding.severity || "OK";
    if (worst === null
        || (SEVERITY_RANK[severity] || 0) > (SEVERITY_RANK[worst] || 0)) {
      worst = severity;
    }
  }
  return worst;
}

// Which rows exist -- not what they say. Values are written in place, so a
// reading that merely moved must not rebuild the strip; only a meter
// appearing or disappearing may.
export function stripStructureSignature(state) {
  const probes = (state && state.probes) || {};
  return JSON.stringify(stripMeters()
    .filter((meter) => readMeter(meter, probes) !== null)
    .map((meter) => meter.id));
}

// --- rendering -------------------------------------------------------------

// Bytes get the pair, which is the whole point. A percentage is already
// self-describing and would only gain a meaningless "/ 100 %".
export function meterValueText(reading) {
  if (reading.unit !== "bytes") return `${formatNumber(reading.value)} %`;
  return translate("ui.value.of_total", {
    value: formatBytes(reading.value), total: formatBytes(reading.capacity) });
}

// A <div> and a <span>, never a canvas: Chart.js sizes itself from its parent
// box (the trap that once left this console a 700px gauge), and a track whose
// height is fixed in CSS cannot get that wrong however wide its container is.
export function meterBar() {
  const track = document.createElement("div");
  track.className = "meter";
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", "100");
  const fill = document.createElement("span");
  fill.className = "meter-fill";
  track.append(fill);
  return track;
}

// A null fraction means there is no share to draw. The bar is hidden rather
// than drawn empty: an empty bar reads as "nothing in use", a different claim.
export function paintBar(track, reading, severity, label) {
  if (reading.fraction === null) { track.hidden = true; return; }
  track.hidden = false;
  const percent = Math.round(reading.fraction * 100);
  const fill = track.firstElementChild;
  fill.style.width = `${percent}%`;
  const wanted = severity ? `meter-fill is-${severity}` : "meter-fill";
  if (fill.className !== wanted) fill.className = wanted;
  track.setAttribute("aria-valuenow", String(percent));
  // The visible label and value are aria-hidden (they would be read twice),
  // so this element carries the whole sentence on its own.
  track.setAttribute("aria-label", label);
  track.setAttribute("aria-valuetext", `${label} — ${meterValueText(reading)}`);
}

let stripRows = new Map();
let lastStripStructure = null;

function buildStripRows(host, probes) {
  clear(host);
  stripRows = new Map();
  for (const meter of stripMeters()) {
    if (readMeter(meter, probes) === null) continue;
    const row = document.createElement("div");
    row.className = "meter-row";
    const label = document.createElement("span");
    label.className = "meter-label text-body-secondary";
    label.setAttribute("aria-hidden", "true");
    const track = meterBar();
    const value = document.createElement("span");
    value.className = "meter-value";
    value.setAttribute("aria-hidden", "true");
    row.append(label, track, value);
    host.append(row);
    stripRows.set(meter.id, { label, track, value });
  }
}

// Simple mode's at-a-glance strip. Values are written into existing rows
// rather than rebuilt, so the 2s tick moves a bar's width and nothing else --
// no teardown, no entry animation, and setText only writes on a real change.
export function renderCapacityStrip(state) {
  const host = el("capacity-strip");
  if (!host) return;
  const probes = (state && state.probes) || {};
  const signature = stripStructureSignature(state);
  if (signature !== lastStripStructure) {
    lastStripStructure = signature;
    buildStripRows(host, probes);
  }
  // An unreadable probe already speaks for itself through Simple mode's
  // "measurement unavailable" card; the strip's card just goes away.
  el("capacity-card").hidden = stripRows.size === 0;
  for (const [id, row] of stripRows) {
    const meter = METERS.find((candidate) => candidate.id === id);
    const reading = readMeter(meter, probes);
    if (!reading) continue;
    const label = metricLabel(meter.id);
    setText(row.label, label);
    setText(row.value, meterValueText(reading));
    paintBar(row.track, reading,
             meterSeverity(meter, state && state.findings), label);
  }
}

// The strip cannot claim a capacity from a measurement that never arrived.
export function clearCapacityStrip() {
  const host = el("capacity-strip");
  if (!host) return;
  clear(host);
  stripRows = new Map();
  lastStripStructure = null;
  el("capacity-card").hidden = true;
}
