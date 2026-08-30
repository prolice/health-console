import test from "node:test";
import assert from "node:assert/strict";

import { formatBytes } from "../i18n.js";

test("formatBytes climbs units and stops at the right one", () => {
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(1024), "1 kB");
  assert.equal(formatBytes(1024 ** 3), "1 GB");
});

test("formatBytes does not promote a value below the next unit", () => {
  assert.equal(formatBytes(1023), "1,023 B");
});
