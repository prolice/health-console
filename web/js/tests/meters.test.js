import test from "node:test";
import assert from "node:assert/strict";

import { METERS, stripMeters, readMeter, meterFraction,
         meterSeverity, stripStructureSignature } from "../meters.js";

function meterById(id) {
  const meter = METERS.find((candidate) => candidate.id === id);
  assert.ok(meter, `no meter declared for ${id}`);
  return meter;
}

test("every meter id is unique", () => {
  const ids = METERS.map((meter) => meter.id);
  assert.equal(new Set(ids).size, ids.length);
});

test("readMeter answers null when the probe is absent", () => {
  assert.equal(readMeter(meterById("mem.available"), {}), null);
});

test("readMeter answers null when the probe is not ok", () => {
  const probes = { memory: { status: "unavailable", reason: "cannot read memory state" } };
  assert.equal(readMeter(meterById("mem.available"), probes), null);
});

test("readMeter reads value and capacity from an ok probe", () => {
  const probes = { memory: { status: "ok", available: 3, total: 12, swap_used: 0, swap_total: 8 } };
  assert.deepEqual(readMeter(meterById("mem.available"), probes),
                   { unit: "bytes", value: 3, capacity: 12, fraction: 0.25 });
});

test("readMeter answers null when a field it needs is missing", () => {
  // swap_total absent (an older server, or a probe shape that changed) must
  // not become a fabricated capacity of zero or NaN.
  const probes = { memory: { status: "ok", available: 3, total: 12, swap_used: 0 } };
  assert.equal(readMeter(meterById("mem.swap.used"), probes), null);
});

test("readMeter answers null when a field is not a finite number", () => {
  const probes = { memory: { status: "ok", available: NaN, total: 12 } };
  assert.equal(readMeter(meterById("mem.available"), probes), null);
});

test("a machine with no swap keeps its zeroes and gets no bar", () => {
  // swap_total 0 is a real, meaningful reading -- "there is no swap here" --
  // and must stay visible as 0 B / 0 B rather than vanish behind an em dash.
  // What it cannot have is a bar: a share of an empty container is undefined.
  const probes = { memory: { status: "ok", available: 3, total: 12, swap_used: 0, swap_total: 0 } };
  assert.deepEqual(readMeter(meterById("mem.swap.used"), probes),
                   { unit: "bytes", value: 0, capacity: 0, fraction: null });
});

test("a percent meter reads against a capacity of 100", () => {
  const probes = { battery: { status: "ok", charge_pct: 82 } };
  assert.deepEqual(readMeter(meterById("battery.charge_pct"), probes),
                   { unit: "percent", value: 82, capacity: 100, fraction: 0.82 });
});

test("the disk meter reads the percentage its metric key names", () => {
  // Not bytes: the byte detail already has a tile of its own next to it
  // (disk.root.free), and this meter's label says "Disk used".
  const probes = { storage: { status: "ok", used: 25, total: 100, free: 75, used_pct: 25 } };
  assert.deepEqual(readMeter(meterById("disk.root.used_pct"), probes),
                   { unit: "percent", value: 25, capacity: 100, fraction: 0.25 });
});

test("meterFraction answers null for a zero or negative capacity", () => {
  assert.equal(meterFraction(0, 0), null);
  assert.equal(meterFraction(5, -1), null);
});

test("meterFraction clamps a value larger than its capacity", () => {
  // available > total should never happen, but a clamp keeps a bad reading
  // from painting a bar wider than its track.
  assert.equal(meterFraction(15, 12), 1);
});

test("meterFraction clamps a negative value to zero", () => {
  assert.equal(meterFraction(-3, 12), 0);
});

test("meterFraction answers null for a non-finite input", () => {
  assert.equal(meterFraction(NaN, 12), null);
  assert.equal(meterFraction(3, Infinity), null);
});

test("a meter stays neutral while the rules engine has opened nothing", () => {
  assert.equal(meterSeverity(meterById("mem.available"), []), null);
});

test("a meter takes the severity of a finding that names it", () => {
  const findings = [{ id: "memory.pressure", severity: "ATTENTION", params: {} }];
  assert.equal(meterSeverity(meterById("mem.available"), findings), "ATTENTION");
  assert.equal(meterSeverity(meterById("mem.swap.used"), findings), "ATTENTION");
});

test("a finding for another metric leaves a meter neutral", () => {
  const findings = [{ id: "storage.root_full", severity: "URGENT", params: {} }];
  assert.equal(meterSeverity(meterById("mem.available"), findings), null);
  assert.equal(meterSeverity(meterById("disk.root.used_pct"), findings), "URGENT");
});

test("a meter named by two open findings takes the worse one", () => {
  const findings = [
    { id: "storage.root_full", severity: "ATTENTION", params: {} },
    { id: "storage.root_full", severity: "URGENT", params: {} },
  ];
  assert.equal(meterSeverity(meterById("disk.root.used_pct"), findings), "URGENT");
});

test("the battery meter is tinted by nothing", () => {
  // findings.py declares no low-charge finding, so there is no verdict to
  // borrow. Inventing a threshold here is exactly what this design refuses.
  assert.deepEqual(meterById("battery.charge_pct").findings, []);
});

test("the strip leaves out cpu.usage", () => {
  // It moves on every 2s tick; a bar that never settles defeats the
  // at-a-glance reading the strip exists for. It keeps its Expert tile bar.
  const ids = stripMeters().map((meter) => meter.id);
  assert.equal(ids.includes("cpu.usage"), false);
  assert.ok(METERS.some((meter) => meter.id === "cpu.usage"));
});

test("the strip carries memory, swap, disk and battery", () => {
  assert.deepEqual(stripMeters().map((meter) => meter.id),
                   ["mem.available", "mem.swap.used", "disk.root.used_pct",
                    "battery.charge_pct"]);
});

test("stripStructureSignature ignores a value that merely moved", () => {
  // Values are written into the existing rows in place; only a change in
  // which rows exist may tear them down and rebuild them.
  const before = { probes: { memory: { status: "ok", available: 3, total: 12, swap_used: 0, swap_total: 8 } } };
  const after = { probes: { memory: { status: "ok", available: 9, total: 12, swap_used: 1, swap_total: 8 } } };
  assert.equal(stripStructureSignature(before), stripStructureSignature(after));
});

test("stripStructureSignature reacts to a probe becoming unavailable", () => {
  const ok = { probes: { memory: { status: "ok", available: 3, total: 12, swap_used: 0, swap_total: 8 } } };
  const gone = { probes: { memory: { status: "unavailable", reason: "no" } } };
  assert.notEqual(stripStructureSignature(ok), stripStructureSignature(gone));
});
