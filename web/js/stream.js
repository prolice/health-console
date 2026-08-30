// SSE connection and freshness tracking. markFreshness reads lastState and
// interfaceTextUnavailable and writes #freshness, so all three stay in this
// module together: splitting the flag from its only reader would break the
// guard that keeps a failed catalogue load visible (see setInterfaceTextUnavailable).

import { el, setText } from "./dom.js";
import { translate, formatTime } from "./i18n.js";

// No SSE event at all for this long: the stream itself is presumed down.
const STALE_AFTER_MS = 15000;
// A measurement older than this is stale even while the stream stays open --
// set well above the 2 s live cadence so ordinary scheduling jitter or a
// single delayed tick never flaps the banner, but a wedged collector that
// keeps re-emitting the same state (see EMPTY_STATE / scheduler.state())
// is still caught rather than shown as "up to date" forever.
const MEASUREMENT_STALE_AFTER_SECONDS = 30;

let lastState = null;
let lastUpdate = Date.now();
let isStale = false;
let interfaceTextUnavailable = false;

export function lastKnownState() { return lastState; }

export function setInterfaceTextUnavailable() {
  interfaceTextUnavailable = true;
}

// A state with no ts/score is not a measurement of anything -- it is the
// scheduler's EMPTY_STATE, seen before the first tick completes (or, in
// principle, if the collector never runs at all). Showing a score or a
// severity for it would be exactly the reassuring lie this console refuses
// to tell.
export function hasMeasurement(state) {
  return Boolean(state) && state.ts != null && state.score != null;
}

// Stale means either signal says so: the stream itself reporting trouble
// (isStale), or -- the defect this guards against -- a stream that keeps
// emitting events on schedule while the measurement inside them stops
// advancing, which a connectivity-only check would never notice.
export function isMeasurementStale(state) {
  if (isStale) return true;
  if (!hasMeasurement(state)) return true;
  return Date.now() / 1000 - state.ts > MEASUREMENT_STALE_AFTER_SECONDS;
}

export function markFreshness(stale) {
  if (interfaceTextUnavailable) {
    // The catalogue never loaded, so translate() has nothing to return
    // but "". If the SSE stream still comes up despite that (a plausible
    // split: static assets down, the API up), a state event must not
    // silently blank out the one visible sign that something is wrong.
    return;
  }
  const zone = el("freshness");
  zone.classList.toggle("stale", stale);
  document.body.classList.toggle("stale", stale);
  if (!hasMeasurement(lastState)) {
    setText(zone, translate("ui.state.no_measurement.detail"));
    return;
  }
  // Always the timestamp of the actual last measurement, never the moment
  // this banner happened to (re)paint -- a locale switch, or a stream that
  // keeps ticking over a wedged collector, must not manufacture freshness
  // that was never there.
  const time = formatTime(new Date(lastState.ts * 1000));
  setText(zone, translate(
    stale ? "ui.freshness.stale" : "ui.freshness.live", { time }));
}

export function connect({ onState, onFreshness }) {
  const source = new EventSource("/api/stream");
  source.addEventListener("state", (event) => {
    lastUpdate = Date.now();
    isStale = false;
    const state = JSON.parse(event.data);
    lastState = state;
    onState(state);
    onFreshness(isMeasurementStale(state));
  });
  source.addEventListener("error", () => {
    isStale = true;
    onFreshness(true);
  });
  setInterval(() => {
    if (Date.now() - lastUpdate > STALE_AFTER_MS) {
      isStale = true;
    }
    // Re-evaluated every tick, not only when the stream just went down:
    // the payload can go stale on its own between SSE events.
    onFreshness(isMeasurementStale(lastState));
  }, 5000);
}
