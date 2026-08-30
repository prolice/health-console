import test from "node:test";
import assert from "node:assert/strict";

import { RANGES, cacheKey } from "../history.js";

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
