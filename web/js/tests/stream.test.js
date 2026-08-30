import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { connect } from "../stream.js";

const JS_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");

// A minimal EventSource stand-in that records every instance constructed
// and lets a test dispatch a named event into whichever listeners were
// registered on it.
class FakeEventSource {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.listeners = {};
    FakeEventSource.instances.push(this);
  }
  addEventListener(type, handler) {
    (this.listeners[type] ||= []).push(handler);
  }
  dispatch(type, data) {
    for (const handler of this.listeners[type] || []) {
      handler({ data: JSON.stringify(data) });
    }
  }
}

// connect() also arms a 5s freshness poll via setInterval, which -- left
// real -- would keep the Node test runner's event loop alive after the
// test itself has finished. Stubbed out here for the same reason fetch and
// localStorage are stubbed in history.test.js: nothing in these tests
// exercises that poll.
function withStubbedEnvironment(run) {
  const hadEventSource = "EventSource" in globalThis;
  const previousEventSource = globalThis.EventSource;
  const previousSetInterval = globalThis.setInterval;
  FakeEventSource.instances = [];
  globalThis.EventSource = FakeEventSource;
  globalThis.setInterval = () => 0;
  try {
    run();
  } finally {
    if (hadEventSource) globalThis.EventSource = previousEventSource;
    else delete globalThis.EventSource;
    globalThis.setInterval = previousSetInterval;
  }
}

test("only one module ever constructs an EventSource, so a page never opens more than one stream", () => {
  // The regression this guards: an action-events listener added by opening
  // a second EventSource("/api/stream") elsewhere (once in app.js, on top
  // of the one connect() already owns) doubles the server threads every
  // open tab costs against MAX_STREAMS -- a shared, per-server cap. This is
  // a static count precisely because the defect was a second *construction
  // site*, not a behavioural difference connect() itself could paper over.
  const files = readdirSync(JS_DIR).filter((name) => name.endsWith(".js"));
  const total = files.reduce((count, name) => {
    const text = readFileSync(path.join(JS_DIR, name), "utf-8");
    return count + (text.match(/new EventSource\(/g) || []).length;
  }, 0);
  assert.equal(total, 1,
    `expected exactly one "new EventSource(" across web/js/*.js, found ${total}`);
});

test("connect() itself opens exactly one EventSource per call", () => {
  withStubbedEnvironment(() => {
    connect({ onState: () => {}, onFreshness: () => {} });
    assert.equal(FakeEventSource.instances.length, 1);
  });
});

test("omitting onAction registers no listener for it, and connect() does not throw", () => {
  withStubbedEnvironment(() => {
    assert.doesNotThrow(() =>
      connect({ onState: () => {}, onFreshness: () => {} }));
    const [source] = FakeEventSource.instances;
    assert.equal((source.listeners.action || []).length, 0);
  });
});

test("onAction, when given, is registered once and receives the parsed payload", () => {
  withStubbedEnvironment(() => {
    let received = null;
    connect({ onState: () => {}, onFreshness: () => {},
             onAction: (event) => { received = event; } });
    const [source] = FakeEventSource.instances;
    assert.equal(source.listeners.action.length, 1);
    source.dispatch("action", { run_id: "run-1", phase: "started" });
    assert.deepEqual(received, { run_id: "run-1", phase: "started" });
  });
});

test("state events still reach onState unaffected by onAction's presence", () => {
  withStubbedEnvironment(() => {
    let state = null;
    connect({ onState: (s) => { state = s; }, onFreshness: () => {},
             onAction: () => {} });
    const [source] = FakeEventSource.instances;
    source.dispatch("state", { ts: 1, score: 90 });
    assert.deepEqual(state, { ts: 1, score: 90 });
  });
});
