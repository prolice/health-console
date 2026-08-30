// History fetching. One cache entry per (metric, window); the server does
// the aggregation, this module only avoids asking twice for the same thing.

import { authHeaders } from "./i18n.js";

// Must match RANGES in healthconsole/server.py -- a mismatch is a 400 the
// user reads as an empty chart. A test asserts the agreement.
export const RANGES = ["1h", "24h", "7d", "90d"];

export class HistoryError extends Error {}

const cache = new Map();

// Unambiguous by construction: a delimiter-joined key collides as soon as
// either component can contain the delimiter, and nothing here enforces
// that they cannot.
export function cacheKey(metric, range) {
  return JSON.stringify([metric, range]);
}

export function clearCache() { cache.clear(); }

export async function fetchSeries(metric, range, { force = false } = {}) {
  const key = cacheKey(metric, range);
  if (!force && cache.has(key)) return cache.get(key);

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

  const series = {
    points: body.points,
    depthDays: body.depth_days,
    table: body.table || "",
  };
  cache.set(key, series);
  return series;
}
