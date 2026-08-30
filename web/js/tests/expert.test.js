import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { chartCards, isDepthShort, depthMessage, renderProbeTable, renderTiles,
         thermalZoneSignature, seriesStyleTokens, TILES } from "../expert.js";

// A minimal stand-in for the DOM, just enough for renderProbeTable() (the
// only exported function here that touches document.createElement /
// el()) to run and be inspected. append()/textContent replicate just
// enough real-DOM behaviour for that: a fresh element has no children,
// append() records them in order, and setting textContent to "" (what
// dom.js's clear() does) drops any children the way a real
// node.textContent = "" assignment would.
class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.className = "";
    this._text = "";
  }
  set textContent(value) {
    this._text = value;
    if (value === "") this.children = [];
  }
  get textContent() { return this._text; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute() {}
}

function withFakeDocument(elementsById, run) {
  const previousDocument = globalThis.document;
  globalThis.document = {
    createElement: (tag) => new FakeElement(tag),
    getElementById: (id) => elementsById[id],
  };
  try {
    run();
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
}

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

test("isDepthShort tolerates rounding noise within the server's own rounding step", () => {
  // depth_days is rounded to two decimals server-side; a depth within half
  // that step of the requested window must not be flagged short because
  // of rounding alone.
  assert.equal(isDepthShort(89.997, "90d"), false);
});

test("isDepthShort catches a real shortfall a looser epsilon would mask", () => {
  // The epsilon must stay small: 0.05 (the original value) is larger than
  // the entire 1h window and made ui.chart.depth_short unreachable on that
  // range. A 90d window actually missing half an hour is a real shortfall,
  // not rounding noise, and must still be reported.
  assert.equal(isDepthShort(89.98, "90d"), true);
});

test("isDepthShort is reachable on the 1h range, unlike with the original 0.05 epsilon", () => {
  // 1/24 day ~= 0.0417; the old 0.05 epsilon meant depthDays + 0.05 was
  // never less than that for any depthDays >= 0, so a console younger
  // than the requested window could never be told so on this range.
  assert.equal(isDepthShort(0.01, "1h"), true);
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

test("depthMessage returns null when the window is fully covered -- no line for a chart that is, in fact, full", () => {
  // The depth line exists to explain a *short* chart. A non-short window
  // used to print its own depth_days instead ("History available: {days}
  // days"), which read as a claim about the machine and contradicted
  // itself across ranges: the same six-months-old machine said "2 days" on
  // the 24h range (raw retention) and "90 days" on the 90d range.
  assert.equal(depthMessage(30, "24h"), null);
  assert.equal(depthMessage(90, "90d"), null);
  // Also true below the one-day threshold: 0.9 day of history against a 1h
  // window is 21x the window asked for, not short, so this must say
  // nothing rather than fall through to some other wording.
  assert.equal(depthMessage(0.9, "1h"), null);
});

test("depthMessage's short-hours sentence never reaches the requested window's own count", () => {
  // 1h requested = 1/24 day ~= 0.0417; 0.02 day (28.8 min) is genuinely
  // short of that.
  const { key, params } = depthMessage(0.02, "1h");
  assert.equal(key, "ui.chart.depth_short_hours");
  assert.equal(params.hours, 0.4);
  assert.ok(params.hours < 1, "the short sentence must not show a full hour");
});

test("depthMessage's short-days sentence never rounds up to the requested window's own count", () => {
  // Reported regression: formatNumber's one-decimal rounding turns 6.97
  // into "7" for display, so a genuinely short 7d window ("Only {days}
  // days of history so far") named the window's own day count. Flooring
  // (not rounding) the number inside the short sentence keeps it under 7
  // whenever the depth genuinely is.
  const { key, params } = depthMessage(6.97, "7d");
  assert.equal(key, "ui.chart.depth_short");
  assert.equal(params.days, 6.9);
  assert.ok(params.days < 7, "the short sentence must not name the full window");
});

test("depthMessage picks the singular hour key when the floored value is exactly one", () => {
  // "Only 1 hours of history so far" is wrong grammar, and reachable
  // whenever flooring lands exactly on 1 -- formatNumber then prints "1"
  // with no decimal, so plural wording reads as a typo. depthDays here is
  // just over 1/24 (one hour), well short of the 90d window requested.
  const { key, params } = depthMessage(1.0008 / 24, "90d");
  assert.equal(key, "ui.chart.depth_short_hour");
  assert.equal(params.hours, 1);
});

test("depthMessage keeps the plural hour key away from exactly one", () => {
  const { key } = depthMessage(0.02, "1h");
  assert.equal(key, "ui.chart.depth_short_hours");
});

test("depthMessage picks the singular day key when the floored value is exactly one", () => {
  const { key, params } = depthMessage(1.05, "90d");
  assert.equal(key, "ui.chart.depth_short_day");
  assert.equal(params.days, 1);
});

test("depthMessage keeps the plural day key away from exactly one", () => {
  const { key } = depthMessage(6.97, "7d");
  assert.equal(key, "ui.chart.depth_short");
});

test("renderProbeTable appends a header row naming the three columns", () => {
  // Kills a mutation that builds the <thead> but never appends it to the
  // table: table.children would then hold only the body rows, and the
  // three columns would be unlabelled to assistive technology.
  const table = new FakeElement("table");
  withFakeDocument({ "probe-table": table }, () => {
    renderProbeTable({ probes: { cpu: { status: "ok" } } });
  });
  const thead = table.children.find((child) => child.tagName === "thead");
  assert.ok(thead, "no <thead> was appended to the probe table");
  assert.equal(thead.children.length, 1, "the header has more or fewer than one row");
  const headerRow = thead.children[0];
  assert.equal(headerRow.children.length, 3, "the header row does not have three columns");
  for (const cell of headerRow.children) {
    assert.equal(cell.tagName, "th", "a header column is not a <th>");
  }
});

test("renderProbeTable still appends one body row per probe alongside the header", () => {
  const table = new FakeElement("table");
  withFakeDocument({ "probe-table": table }, () => {
    renderProbeTable({ probes: { cpu: { status: "ok" }, battery: { status: "unavailable" } } });
  });
  const tbody = table.children.find((child) => child.tagName === "tbody");
  assert.ok(tbody, "no <tbody> was appended to the probe table");
  assert.equal(tbody.children.length, 2, "expected one row per probe");
});

test("renderTiles gives every tile a full-width row below md and a grid " +
     "cell from md up", () => {
  // col-12: a single-column, full-width row below the md breakpoint (see
  // the coordinator's note that six cramped grid cells on a phone would
  // read worse than the tiles carried no explanation at all). col-md-6
  // and col-xl-4 restore a real grid -- 2, then 3, tiles across -- once
  // there is room for one. Never 6-across: with the page capped at
  // .view-wide's max width, a six-across cell's text column can never
  // reach the width a two-across cell has even at its narrowest, so a
  // six-across stage would recreate the same wall of wrapped text this
  // reflow exists to avoid, right at the most common viewport this
  // console is read at (an ordinary laptop).
  const row = new FakeElement("div");
  withFakeDocument({ "expert-tiles": row }, () => {
    renderTiles({ probes: {} });
  });
  const columns = row.children.filter((child) => child.tagName === "div");
  assert.equal(columns.length, TILES.length,
    "expected one column per tile in TILES");
  for (const column of columns) {
    assert.equal(column.className, "col-12 col-md-6 col-xl-4");
  }
});

test("renderTiles puts the label and value in a flippable row, and the " +
     "hint in its own paragraph below it", () => {
  const row = new FakeElement("div");
  withFakeDocument({ "expert-tiles": row }, () => {
    renderTiles({ probes: {} });
  });
  const [column] = row.children.filter((child) => child.tagName === "div");
  const [card] = column.children;
  const [body] = card.children;
  // Two children: the label+value row, then the hint -- not folded into
  // one paragraph, which is what would make the hint impossible to style
  // (or hide) independently of the reading.
  assert.equal(body.children.length, 2);
  const [head, hint] = body.children;
  // d-flex (a row, below md) that becomes d-md-block (stacked, from md
  // up) is what lets the same markup serve a phone's list row and a
  // laptop's grid cell -- see the coordinator's reflow note.
  assert.match(head.className, /\bd-flex\b/);
  assert.match(head.className, /\bd-md-block\b/);
  assert.equal(head.children.length, 2, "expected a label and a value");
  // The hint is a plain, always-rendered paragraph -- never a title
  // attribute, which a touch or keyboard user has no way to reveal.
  assert.equal(hint.tagName, "p");
  assert.match(hint.className, /\bsmall\b/);
  assert.match(hint.className, /\btext-body-secondary\b/);
});

test("renderTiles gives every tile in TILES its own hint catalogue key", () => {
  // Nothing derives the hint keys from TILES anywhere else --
  // tests/test_i18n.py keeps its own hand-written list -- so this is the
  // only check standing between a seventh tile and a shipped, empty
  // caption. Read the real catalogues renderTiles() itself pulls
  // `ui.metric.${metric}.hint` from (see expert.js) and require the key to
  // exist, with real text, for every entry in TILES, in both locales.
  const i18nDir = new URL("../../i18n/", import.meta.url);
  const en = JSON.parse(readFileSync(new URL("en.json", i18nDir), "utf8"));
  const fr = JSON.parse(readFileSync(new URL("fr.json", i18nDir), "utf8"));
  for (const [metric] of TILES) {
    const key = `ui.metric.${metric}.hint`;
    for (const [locale, catalogue] of [["en", en], ["fr", fr]]) {
      assert.equal(typeof catalogue[key], "string",
        `${locale} catalogue is missing ${key}`);
      assert.ok(catalogue[key].length > 0, `${locale}'s ${key} is empty`);
    }
  }
});

test("thermalZoneSignature changes when the discovered zone set changes", () => {
  // This is the primitive redrawCharts()/renderExpert() compare to decide
  // whether to redraw the thermal card outside the normal trigger points
  // (see expert.js's top-of-file comment): a machine whose /api/now failed
  // falls back to cpu.temp.pkg alone, and this must return a different
  // value once the real zones become known from the first SSE state.
  const before = thermalZoneSignature({ probes: { thermal: { status: "ok", zones: {} } } });
  const after = thermalZoneSignature({
    probes: { thermal: { status: "ok", zones: { acpitz: 40, coretemp: 55 } } },
  });
  assert.notEqual(before, after);
});

test("thermalZoneSignature does not change when the same zones repeat tick to tick", () => {
  // This is what keeps the zone-change redraw from firing on every 2s
  // tick: two states with the same zones (rebuilt fresh each time, as the
  // scheduler does, and in a different key order) must compare equal.
  const first = thermalZoneSignature({
    probes: { thermal: { status: "ok", zones: { acpitz: 40, coretemp: 55 } } },
  });
  const second = thermalZoneSignature({
    probes: { thermal: { status: "ok", zones: { coretemp: 56, acpitz: 41 } } },
  });
  assert.equal(first, second);
});

test("thermalZoneSignature agrees with chartCards on when there are no zones to draw", () => {
  // Both a missing state and an unavailable thermal probe fall back to
  // cpu.temp.pkg alone in chartCards(); the signature for both must be the
  // same "no zones" value so a transition between them is not mistaken for
  // a zone change worth a redraw.
  const noState = thermalZoneSignature(undefined);
  const unavailable = thermalZoneSignature({ probes: { thermal: { status: "unavailable" } } });
  const emptyZones = thermalZoneSignature({ probes: { thermal: { status: "ok", zones: {} } } });
  assert.equal(noState, unavailable);
  assert.equal(unavailable, emptyZones);
});

test("seriesStyleTokens gives no two of the first sixteen series the same (colour, dash) pair", () => {
  // A typical laptop's thermal card draws cpu.temp.pkg plus one series per
  // hwmon zone -- five to eight zones is not unusual, i.e. six to nine
  // series. A regression that cycles colour and dash on the same period
  // (index % 4 for both) makes series 4 byte-identical in style to series
  // 0, which this guards against well past that range.
  const seen = new Set();
  for (let index = 0; index < 16; index += 1) {
    const { colour, borderDash } = seriesStyleTokens(index);
    const key = JSON.stringify([colour, borderDash]);
    assert.ok(!seen.has(key), `index ${index} repeats an earlier (colour, dash) pair`);
    seen.add(key);
  }
});

test("seriesStyleTokens varies both colour and dash across the first four series", () => {
  // The regression this replaced (all four solid, distinguished by colour
  // alone) broke "colour never carries information alone"; the fix must not
  // just move the duplicate further out.
  const styles = [0, 1, 2, 3].map(seriesStyleTokens);
  assert.equal(new Set(styles.map((s) => s.colour)).size, 4);
  assert.equal(new Set(styles.map((s) => JSON.stringify(s.borderDash))).size, 4);
});
