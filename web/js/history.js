// History fetching. One cache entry per (metric, window); the server does
// the aggregation, this module only avoids asking twice for the same thing
// -- and only for as long as that thing is still likely to be current.

import { authHeaders } from "./i18n.js";

// Must match RANGES in healthconsole/server.py -- a mismatch is a 400 the
// user reads as an empty chart. A test asserts the agreement.
export const RANGES = ["1h", "24h", "7d", "90d"];

export class HistoryError extends Error {}

const cache = new Map();

// The only clock this module reads; a mutable object so a test can stub
// clock.now (and must restore it) without a real timer.
export const clock = { now: () => Date.now() };

// Unambiguous by construction: a delimiter-joined key collides as soon as
// either component can contain the delimiter, and nothing here enforces
// that they cannot.
export function cacheKey(metric, range) {
  return JSON.stringify([metric, range]);
}

export function clearCache() { cache.clear(); }

// Window length in seconds, keyed like RANGES.
const RANGE_SECONDS = { "1h": 3600, "24h": 86400, "7d": 604800, "90d": 7776000 };

// A cached series need not refresh as often as the data changes: a 90 d
// window gains nothing from being re-fetched every couple of seconds. But
// an unbounded cache is the reported bug -- a Simple-mode sparkline frozen
// at page-load time under a banner claiming the page is current. A
// fortieth of the window surfaces a change well within a normal viewing
// session (~90s on the shortest range, ~54h on the longest), floored at
// 30s so the shortest range cannot turn into a request storm.
const MIN_TTL_MS = 30_000;
const TTL_FRACTION = 1 / 40;

export function cacheTtlMs(range) {
  const seconds = RANGE_SECONDS[range];
  return seconds ? Math.max(MIN_TTL_MS, seconds * 1000 * TTL_FRACTION) : MIN_TTL_MS;
}

// Pure, and exported, so the staleness decision itself -- not just its
// effect buried inside fetchSeries() -- can be unit-tested directly.
export function isStale(fetchedAt, range, now = clock.now()) {
  return now - fetchedAt >= cacheTtlMs(range);
}

export async function fetchSeries(metric, range, { force = false } = {}) {
  const key = cacheKey(metric, range);
  const cached = cache.get(key);
  if (!force && cached && !isStale(cached.fetchedAt, range)) return cached.series;

  let response;
  try {
    response = await fetch(
      `/api/history?metric=${encodeURIComponent(metric)}`
      + `&range=${encodeURIComponent(range)}`,
      { headers: authHeaders() });
  } catch (cause) {
    // A network failure is not an empty series: the caller must be able to
    // tell "nothing was recorded" from "we could not ask".
    throw new HistoryError(`network failure for ${metric}`, { cause });
  }
  if (!response.ok) {
    throw new HistoryError(`${metric} ${range}: HTTP ${response.status}`);
  }

  let body;
  try {
    body = await response.json();
  } catch (cause) {
    // A malformed response body (truncated, corrupt, or invalid JSON) is not
    // an empty series: it is a "we could not ask" failure.
    throw new HistoryError(`malformed JSON response for ${metric} ${range}`, { cause });
  }

  // Validate the shape before constructing a series. A 200 with missing or
  // null fields would be indistinguishable from a genuinely empty window when
  // cached, making the console state a falsehood about the machine.
  if (!Array.isArray(body.points)) {
    throw new HistoryError(`${metric} ${range}: points is not an array`);
  }
  if (typeof body.depth_days !== "number") {
    throw new HistoryError(`${metric} ${range}: depth_days is not a number`);
  }

  const series = { points: body.points, depthDays: body.depth_days };
  cache.set(key, { series, fetchedAt: clock.now() });
  return series;
}
