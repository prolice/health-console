import test from "node:test";
import assert from "node:assert/strict";

import { RANGES, cacheKey, cacheTtlMs, isStale, fetchSeries, clearCache,
         clock, HistoryError } from "../history.js";

// fetchSeries() reads two globals history.js does not otherwise let a test
// control: fetch (via authHeaders()'s caller) and, transitively through
// authHeaders() itself, localStorage. Neither exists in the Node test
// runner, so both are stubbed for the duration of the callback and
// restored afterwards (deleted if they were not defined before) so a test
// here cannot leak a fake fetch or clock into an unrelated test file.
async function withStubbedEnvironment(fetchImpl, run) {
  const hadFetch = "fetch" in globalThis;
  const previousFetch = globalThis.fetch;
  const hadLocalStorage = "localStorage" in globalThis;
  const previousLocalStorage = globalThis.localStorage;
  const previousNow = clock.now;
  globalThis.fetch = fetchImpl;
  globalThis.localStorage = { getItem: () => null, setItem: () => {} };
  clearCache();
  try {
    await run();
  } finally {
    if (hadFetch) globalThis.fetch = previousFetch; else delete globalThis.fetch;
    if (hadLocalStorage) globalThis.localStorage = previousLocalStorage;
    else delete globalThis.localStorage;
    clock.now = previousNow;
    clearCache();
  }
}

function jsonResponse(body) {
  return { ok: true, status: 200, json: async () => body };
}

test("the four windows the server accepts are declared", () => {
  assert.deepEqual([...RANGES].sort(), ["1h", "24h", "7d", "90d"].sort());
});

test("cache keys separate metric from range unambiguously", () => {
  // Ordinary case: same metric, different ranges
  assert.notEqual(cacheKey("cpu.usage", "1h"), cacheKey("cpu.usage", "24h"));
  // Ambiguous case with string concat: "a" + "b c" vs "a b" + "c" both yield
  // "a b c", but JSON.stringify distinguishes them.
  assert.notEqual(cacheKey("a", "b c"), cacheKey("a b", "c"));
});

test("cacheTtlMs grows with the window but never below the floor", () => {
  // 1h/40 = 90s, comfortably above the 30s floor.
  assert.equal(cacheTtlMs("1h"), 90_000);
  // A longer window earns a longer TTL -- there is nothing to gain from
  // re-fetching a 90d chart as often as a 1h one.
  assert.ok(cacheTtlMs("24h") > cacheTtlMs("1h"));
  assert.ok(cacheTtlMs("7d") > cacheTtlMs("24h"));
  assert.ok(cacheTtlMs("90d") > cacheTtlMs("7d"));
});

test("cacheTtlMs falls back to the floor for an unrecognised range", () => {
  assert.equal(cacheTtlMs("unknown"), 30_000);
});

test("isStale is false for an entry younger than its range's TTL", () => {
  const now = 1_000_000;
  assert.equal(isStale(now - 1000, "1h", now), false);
});

test("isStale is true once an entry reaches its range's TTL", () => {
  // The 1h window's TTL is 90s (see cacheTtlMs); this is the reported bug
  // itself -- a Simple-mode sparkline that froze at page-load time while
  // the freshness banner kept claiming the page was current.
  const now = 1_000_000;
  const ttl = cacheTtlMs("1h");
  assert.equal(isStale(now - ttl, "1h", now), true);
  assert.equal(isStale(now - (ttl - 1), "1h", now), false);
});

test("isStale treats a 1h window very differently from a 90d one at the same age", () => {
  // The exact scenario Task 8 exists to fix: an entry fetched 5 minutes ago
  // is long stale on the 1h range but nowhere near stale on the 90d one.
  const now = 1_000_000;
  const fiveMinutesAgo = now - 5 * 60 * 1000;
  assert.equal(isStale(fiveMinutesAgo, "1h", now), true);
  assert.equal(isStale(fiveMinutesAgo, "90d", now), false);
});

test("fetchSeries cache hit: two calls inside the TTL issue exactly one request", async () => {
  let calls = 0;
  let currentTime = 1_000_000;
  await withStubbedEnvironment(
    async () => { calls += 1; return jsonResponse({ points: [[0, 1]], depth_days: 1 }); },
    async () => {
      clock.now = () => currentTime;
      const first = await fetchSeries("cpu.usage", "1h");
      currentTime += cacheTtlMs("1h") - 1; // still fresh
      const second = await fetchSeries("cpu.usage", "1h");
      assert.equal(calls, 1);
      assert.deepEqual(second, first);
      assert.deepEqual(second, { points: [[0, 1]], depthDays: 1 });
    });
});

test("fetchSeries cache miss on expiry: a stale entry is refetched and the new data wins", async () => {
  let calls = 0;
  let currentTime = 1_000_000;
  await withStubbedEnvironment(
    async () => {
      calls += 1;
      return calls === 1
        ? jsonResponse({ points: [[0, 1]], depth_days: 1 })
        : jsonResponse({ points: [[0, 2]], depth_days: 2 });
    },
    async () => {
      clock.now = () => currentTime;
      const first = await fetchSeries("cpu.usage", "1h");
      currentTime += cacheTtlMs("1h"); // exactly stale, see isStale's >= boundary
      const second = await fetchSeries("cpu.usage", "1h");
      assert.equal(calls, 2);
      assert.deepEqual(first, { points: [[0, 1]], depthDays: 1 });
      assert.deepEqual(second, { points: [[0, 2]], depthDays: 2 });
    });
});

test("fetchSeries force:true bypasses a still-fresh entry", async () => {
  let calls = 0;
  const currentTime = 1_000_000;
  await withStubbedEnvironment(
    async () => {
      calls += 1;
      return calls === 1
        ? jsonResponse({ points: [[0, 1]], depth_days: 1 })
        : jsonResponse({ points: [[0, 9]], depth_days: 9 });
    },
    async () => {
      clock.now = () => currentTime;
      await fetchSeries("cpu.usage", "1h");
      const forced = await fetchSeries("cpu.usage", "1h", { force: true });
      assert.equal(calls, 2);
      assert.deepEqual(forced, { points: [[0, 9]], depthDays: 9 });
    });
});

test("fetchSeries throws HistoryError on a post-expiry failure rather than serving the stale entry", async () => {
  let calls = 0;
  let currentTime = 1_000_000;
  await withStubbedEnvironment(
    async () => {
      calls += 1;
      if (calls === 1) return jsonResponse({ points: [[0, 1]], depth_days: 1 });
      throw new Error("network down");
    },
    async () => {
      clock.now = () => currentTime;
      await fetchSeries("cpu.usage", "1h");
      currentTime += cacheTtlMs("1h"); // now stale
      await assert.rejects(
        () => fetchSeries("cpu.usage", "1h"),
        (error) => error instanceof HistoryError);
      // The failed refetch must not have quietly served (or poisoned the
      // cache with) the old, now-stale entry: a later successful call still
      // gets to try the network rather than being told the value is settled.
      assert.equal(calls, 2);
    });
});
