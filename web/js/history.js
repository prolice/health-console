// History fetching. One cache entry per (metric, window); the server does
// the aggregation, this module only avoids asking twice for the same thing.

import { authHeaders } from "./i18n.js";

// Must match RANGES in healthconsole/server.py -- a mismatch is a 400 the
// user reads as an empty chart. A test asserts the agreement.
export const RANGES = ["1h", "24h", "7d", "90d"];

export class HistoryError extends Error {}

const cache = new Map();

export function cacheKey(metric, range) { return `${metric} ${range}`; }

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
  const body = await response.json();
  const series = {
    points: body.points || [],
    depthDays: body.depth_days ?? 0,
    table: body.table || "",
  };
  cache.set(key, series);
  return series;
}
