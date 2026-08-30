import test from "node:test";
import assert from "node:assert/strict";

import { summarise, shouldDecimate, metricLabel } from "../charts.js";

test("summarise reports the extremes and the latest value", () => {
  const points = [[100, 12], [160, 87], [220, 34]];
  assert.deepEqual(summarise(points), { min: 12, max: 87, current: 34 });
});

test("summarise returns null rather than inventing zeros for no data", () => {
  // A chart with no points must say "nothing recorded", never draw a flat
  // line at zero -- a flat line is a measurement, absence is not.
  assert.equal(summarise([]), null);
  assert.equal(summarise(undefined), null);
});

test("summarise ignores null samples rather than treating them as zero", () => {
  assert.deepEqual(summarise([[100, null], [160, 5], [220, 9]]),
                   { min: 5, max: 9, current: 9 });
});

test("summarise reports the last finite sample as current, not the last bucket", () => {
  // The freshest bucket can be empty (a collector hiccup, or simply not yet
  // populated); current must be the most recent value that was actually
  // recorded, not that empty trailing bucket.
  assert.deepEqual(summarise([[100, 5], [160, 9], [220, null]]),
                   { min: 5, max: 9, current: 9 });
});

test("summarise of all-null samples is null, not zeros", () => {
  assert.equal(summarise([[1, null], [2, null]]), null);
});

test("decimation switches on above a thousand points", () => {
  assert.equal(shouldDecimate(new Array(999).fill([0, 0])), false);
  assert.equal(shouldDecimate(new Array(1001).fill([0, 0])), true);
});

test("metricLabel falls back to the raw key when the catalogue has no entry", () => {
  // Thermal zone keys (thermal.acpitz, thermal.x86_pkg_temp, ...) are
  // discovered from the kernel at runtime and cannot have catalogue entries.
  assert.equal(metricLabel("thermal.acpitz"), "thermal.acpitz");
});
