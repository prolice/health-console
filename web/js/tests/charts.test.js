import test from "node:test";
import assert from "node:assert/strict";

import { summarise, shouldDecimate, metricLabel,
         drawSparkline, destroyIn, destroyAll } from "../charts.js";

// A minimal stand-in for Chart.js: enough of its static getChart() registry
// and instance destroy() for destroyIn()/destroyAll() to be exercised
// without a real canvas or a bundler-free browser.
class FakeChart {
  constructor(canvas) {
    this.canvas = canvas;
    this.destroyCount = 0;
    FakeChart.registry.set(canvas, this);
  }
  destroy() {
    this.destroyCount += 1;
    FakeChart.registry.delete(this.canvas);
  }
}
FakeChart.registry = new Map();
FakeChart.getChart = (canvas) => FakeChart.registry.get(canvas);

function withFakeChart(run) {
  const previousChart = globalThis.Chart;
  const previousMatchMedia = globalThis.matchMedia;
  globalThis.Chart = FakeChart;
  // baseOptions() reads prefers-reduced-motion on every draw; Node has no
  // real matchMedia, so a bare stand-in is needed to exercise draw*() at all.
  globalThis.matchMedia = () => ({ matches: false });
  try {
    run();
  } finally {
    globalThis.Chart = previousChart;
    globalThis.matchMedia = previousMatchMedia;
  }
}

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

test("destroyIn destroys the chart living in a canvas and forgets it", () => {
  withFakeChart(() => {
    const canvas = {};
    const chart = drawSparkline(canvas, [[0, 1], [1, 2]], "#000");
    assert.ok(chart, "drawSparkline did not construct a chart");

    const container = {
      querySelectorAll: (selector) => (selector === "canvas" ? [canvas] : []),
    };
    destroyIn(container);
    assert.equal(chart.destroyCount, 1, "destroyIn did not destroy the chart");

    // renderSimple() calls destroyIn() before every rebuild. If destroyIn
    // left the instance in charts.js's own `live` registry, the next
    // destroyAll() (or a later destroyIn() pass) would destroy it a second
    // time -- the exact bookkeeping bug the brief warns against.
    destroyAll();
    assert.equal(chart.destroyCount, 1,
                "destroyAll destroyed a chart destroyIn had already removed");
  });
});

test("destroyIn is a no-op when the chart library never loaded", () => {
  const previousChart = globalThis.Chart;
  delete globalThis.Chart;
  try {
    const container = {
      querySelectorAll: () => { throw new Error("must not query when charts are unavailable"); },
    };
    assert.doesNotThrow(() => destroyIn(container));
  } finally {
    globalThis.Chart = previousChart;
  }
});
