import test from "node:test";
import assert from "node:assert/strict";

import { RANGES, cacheKey, cacheTtlMs, isStale } from "../history.js";

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
