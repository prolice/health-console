import test from "node:test";
import assert from "node:assert/strict";

import { FINDING_METRIC, findingsSignature } from "../simple.js";

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
