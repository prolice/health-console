import test from "node:test";
import assert from "node:assert/strict";

import { FINDING_METRIC, findingsSignature, gaugeSignature } from "../simple.js";

test("battery.incoherent maps to no metric", () => {
  // The console has just declared those readings untrustworthy; drawing a
  // curve from them would take that claim back in the same breath.
  assert.equal(Object.hasOwn(FINDING_METRIC, "battery.incoherent"), false);
});

test("findingsSignature is stable for an unchanged state", () => {
  const state = {
    findings: [{ id: "cpu.usage_high", severity: "ATTENTION", params: { usage_pct: 91 } }],
    probes: { battery: { status: "unavailable" } },
  };
  assert.equal(findingsSignature(state), findingsSignature(state));
  // A fresh object with the same content must hash the same way: the
  // signature is a fingerprint of content, not of object identity.
  const clone = JSON.parse(JSON.stringify(state));
  assert.equal(findingsSignature(state), findingsSignature(clone));
});

test("findingsSignature changes when a finding's severity changes", () => {
  const base = { findings: [{ id: "cpu.usage_high", severity: "ATTENTION", params: {} }], probes: {} };
  const escalated = { findings: [{ id: "cpu.usage_high", severity: "URGENT", params: {} }], probes: {} };
  assert.notEqual(findingsSignature(base), findingsSignature(escalated));
});

test("findingsSignature changes when a finding's parameters change", () => {
  const before = { findings: [{ id: "cpu.usage_high", severity: "ATTENTION", params: { usage_pct: 91 } }], probes: {} };
  const after = { findings: [{ id: "cpu.usage_high", severity: "ATTENTION", params: { usage_pct: 95 } }], probes: {} };
  assert.notEqual(findingsSignature(before), findingsSignature(after));
});

test("findingsSignature ignores a healthy probe with no eval_error", () => {
  // Only probes that actually render a card (unavailable, or ok with an
  // eval_error) may affect what #findings shows.
  const healthy = { findings: [], probes: { cpu: { status: "ok" } } };
  const alsoHealthy = { findings: [], probes: {} };
  assert.equal(findingsSignature(healthy), findingsSignature(alsoHealthy));
});

test("findingsSignature reacts to a probe becoming unavailable", () => {
  const ok = { findings: [], probes: { battery: { status: "ok" } } };
  const gone = { findings: [], probes: { battery: { status: "unavailable", reason: "no sysfs entry" } } };
  assert.notEqual(findingsSignature(ok), findingsSignature(gone));
});

test("gaugeSignature is stable for an unchanged score and severity", () => {
  const state = { ts: 100, score: 87, severity: "INFO" };
  assert.equal(gaugeSignature(state), gaugeSignature({ ...state }));
});

test("gaugeSignature changes when the score changes", () => {
  const before = { ts: 100, score: 87, severity: "INFO" };
  const after = { ts: 160, score: 82, severity: "INFO" };
  assert.notEqual(gaugeSignature(before), gaugeSignature(after));
});

test("gaugeSignature changes when the severity changes", () => {
  // The severity, not just the score, picks the gauge's colour.
  const before = { ts: 100, score: 70, severity: "ATTENTION" };
  const after = { ts: 160, score: 70, severity: "URGENT" };
  assert.notEqual(gaugeSignature(before), gaugeSignature(after));
});

test("gaugeSignature of no-measurement never equals a real score", () => {
  // hasMeasurement() (stream.js) treats a state with no ts/score as no
  // measurement at all -- the scheduler's EMPTY_STATE. Its gauge signature
  // must not collapse onto a real score by accident (e.g. both stringifying
  // to a falsy-ish value): a transition into or out of that state has to
  // force the canvas to be redrawn, since it was torn down in between.
  const noMeasurement = {};
  const real = { ts: 100, score: 0, severity: "URGENT" };
  assert.notEqual(gaugeSignature(noMeasurement), gaugeSignature(real));
  assert.equal(gaugeSignature(noMeasurement), null);
});
