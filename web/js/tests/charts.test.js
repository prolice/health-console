import test from "node:test";
import assert from "node:assert/strict";

import { summarise, shouldDecimate } from "../charts.js";

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

test("decimation switches on above a thousand points", () => {
  assert.equal(shouldDecimate(new Array(999).fill([0, 0])), false);
  assert.equal(shouldDecimate(new Array(1001).fill([0, 0])), true);
});
