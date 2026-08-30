import test from "node:test";
import assert from "node:assert/strict";

import { errorKeyForStatus, outcomeKey, shouldTrackEvent, reconcileRun,
         isRunDisabled, renderActionRows, renderAuditRows } from "../actions.js";

// A minimal stand-in for the DOM, just enough for the two exported row
// builders (the only functions here that touch document.createElement /
// el()) to run and be inspected -- the same technique expert.test.js uses
// for renderProbeTable().
class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.className = "";
    this.dataset = {};
    this._text = "";
  }
  set textContent(value) {
    this._text = value;
    if (value === "") this.children = [];
  }
  get textContent() { return this._text; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute() {}
  addEventListener() {}
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

test("errorKeyForStatus maps the three documented statuses, and only those", () => {
  assert.equal(errorKeyForStatus(409), "ui.error.action.busy");
  assert.equal(errorKeyForStatus(403), "ui.error.action.refused");
  assert.equal(errorKeyForStatus(401), "ui.error.action.failed");
  assert.equal(errorKeyForStatus(500), "ui.error.action.failed");
  assert.equal(errorKeyForStatus(404), "ui.error.action.failed");
});

test("outcomeKey never collapses a killed run onto a successful one", () => {
  // exit_code is null exactly for a killed run and is never 0 for that --
  // the two must render through different keys.
  assert.equal(outcomeKey(null), "ui.actions.result.killed");
  assert.equal(outcomeKey(0), "ui.actions.result.ok");
  assert.notEqual(outcomeKey(null), outcomeKey(0));
});

test("outcomeKey treats any non-zero code as a failure", () => {
  assert.equal(outcomeKey(1), "ui.actions.result.failed");
  assert.equal(outcomeKey(130), "ui.actions.result.failed");
});

test("shouldTrackEvent ignores everything until a run is being tracked", () => {
  assert.equal(shouldTrackEvent({ run_id: "a" }, null), false);
});

test("shouldTrackEvent demultiplexes on run_id", () => {
  // A slow listener can let one run's event arrive after a different run
  // has since started -- only the run_id this tab is actually tracking may
  // touch the live pane or the button state.
  assert.equal(shouldTrackEvent({ run_id: "run-1" }, "run-2"), false);
  assert.equal(shouldTrackEvent({ run_id: "run-2" }, "run-2"), true);
});

test("reconcileRun prefers the audit row over the finished event's own fields", () => {
  const event = { run_id: "run-1", exit_code: 1, duration_ms: 999 };
  const runs = [{ id: "run-1", exit_code: 0, duration_ms: 1234, output: "ok\n" }];
  assert.deepEqual(reconcileRun(event, runs),
    { exitCode: 0, durationMs: 1234, output: "ok\n" });
});

test("reconcileRun falls back to the event when no row was written", () => {
  // healthconsole/runner.py notes the run lock can still release even when
  // the audit-row write itself failed -- the live pane's own state is all
  // that is left in that case, so output must not be blanked to null-turned-
  // empty-string, and the event's own fields still surface something.
  const event = { run_id: "run-1", exit_code: null, duration_ms: 500 };
  const result = reconcileRun(event, []);
  assert.deepEqual(result, { exitCode: null, durationMs: 500, output: null });
});

test("reconcileRun never confuses a different run's row for this one", () => {
  const event = { run_id: "run-2", exit_code: 0, duration_ms: 10 };
  const runs = [{ id: "run-1", exit_code: 1, duration_ms: 999, output: "wrong run" }];
  const result = reconcileRun(event, runs);
  assert.equal(result.output, null);
  assert.equal(result.exitCode, 0);
});

test("isRunDisabled gates on availability and on a run already in flight", () => {
  assert.equal(isRunDisabled({ available: true }, false), false);
  assert.equal(isRunDisabled({ available: false }, false), true);
  assert.equal(isRunDisabled({ available: true }, true), true);
});

test("renderActionRows draws one row per action and disables the unavailable one", () => {
  const list = new FakeElement("div");
  withFakeDocument({ "action-list": list }, () => {
    renderActionRows(
      [{ id: "apt.refresh", risk: "safe", available: true },
       { id: "other.thing", risk: "medium", available: false }],
      false);
  });
  assert.equal(list.children.length, 2);
  const buttons = list.children.map((row) =>
    row.children.find((child) => child.tagName === "button"));
  assert.equal(buttons[0].disabled, false);
  assert.equal(buttons[1].disabled, true);
});

test("renderActionRows disables every button while a run is in flight", () => {
  const list = new FakeElement("div");
  withFakeDocument({ "action-list": list }, () => {
    renderActionRows([{ id: "apt.refresh", risk: "safe", available: true }], true);
  });
  const button = list.children[0].children.find((child) => child.tagName === "button");
  assert.equal(button.disabled, true);
});

test("renderActionRows shows the empty state rather than an empty panel", () => {
  const list = new FakeElement("div");
  withFakeDocument({ "action-list": list }, () => {
    renderActionRows([], false);
  });
  assert.equal(list.children.length, 1);
});

test("renderAuditRows draws a header row and one row per run", () => {
  const table = new FakeElement("table");
  withFakeDocument({ "audit-table": table }, () => {
    renderAuditRows([
      { id: "run-1", ts: 100, action_id: "apt.refresh", source: "127.0.0.1",
        exit_code: 0, duration_ms: 500, output: "done" },
    ]);
  });
  const [head, body] = table.children;
  assert.equal(head.tagName, "thead");
  assert.equal(head.children[0].children.length, 4);
  assert.equal(body.children.length, 1);
  assert.equal(body.children[0].children.length, 4);
});

test("renderAuditRows shows the empty state rather than an empty table", () => {
  const table = new FakeElement("table");
  withFakeDocument({ "audit-table": table }, () => {
    renderAuditRows([]);
  });
  const [, body] = table.children;
  assert.equal(body.children.length, 1);
  assert.equal(body.children[0].children[0].colSpan, 4);
});
