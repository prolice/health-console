import test from "node:test";
import assert from "node:assert/strict";

import { chartCards, isDepthShort, TILES } from "../expert.js";

test("chartCards draws a metric for every zone the live state reports", () => {
  const state = {
    probes: { thermal: { status: "ok", package_c: 55,
                         zones: { acpitz: 40, x86_pkg_temp: 55 } } },
  };
  const [, metrics] = chartCards(state)
    .find(([titleKey]) => titleKey === "ui.chart.group.thermal");
  assert.deepEqual(metrics, ["cpu.temp.pkg", "thermal.acpitz", "thermal.x86_pkg_temp"]);
});

test("chartCards still draws cpu.temp.pkg alone when the kernel reports no zones", () => {
  const [, metrics] = chartCards({ probes: { thermal: { status: "ok", zones: {} } } })
    .find(([titleKey]) => titleKey === "ui.chart.group.thermal");
  assert.deepEqual(metrics, ["cpu.temp.pkg"]);
});

test("chartCards falls back to cpu.temp.pkg alone when thermal itself is unavailable", () => {
  const unavailable = chartCards({ probes: { thermal: { status: "unavailable", reason: "x" } } })
    .find(([titleKey]) => titleKey === "ui.chart.group.thermal");
  assert.deepEqual(unavailable[1], ["cpu.temp.pkg"]);

  // Also true before any state has ever arrived (mode switched to Expert
  // before the first SSE event).
  const noState = chartCards(undefined)
    .find(([titleKey]) => titleKey === "ui.chart.group.thermal");
  assert.deepEqual(noState[1], ["cpu.temp.pkg"]);
});

test("isDepthShort flags a window shorter than what was requested", () => {
  assert.equal(isDepthShort(6, "90d"), true);
  assert.equal(isDepthShort(90, "90d"), false);
  assert.equal(isDepthShort(1, "24h"), false);
});

test("isDepthShort tolerates rounding noise just under the requested window", () => {
  // depth_days is rounded to two decimals server-side; a window that
  // genuinely covers the request must not be flagged short because of it.
  assert.equal(isDepthShort(89.98, "90d"), false);
});

test("a tile reads null, not a fabricated zero, when its probe is unavailable", () => {
  const probes = { cpu: { status: "unavailable", reason: "boom" } };
  const [, read] = TILES.find(([metric]) => metric === "cpu.usage");
  assert.equal(read(probes), null);
});

test("a tile reads the live value when its probe is ok", () => {
  const probes = { cpu: { status: "ok", usage_pct: 42, load1: 1.5 } };
  const usage = TILES.find(([metric]) => metric === "cpu.usage")[1];
  const load1 = TILES.find(([metric]) => metric === "load.1")[1];
  assert.equal(usage(probes), 42);
  assert.equal(load1(probes), 1.5);
});

test("a tile reads null when the probe it depends on is entirely absent", () => {
  const [, read] = TILES.find(([metric]) => metric === "battery.charge_pct");
  assert.equal(read({}), null);
});
