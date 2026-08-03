import assert from "node:assert/strict";
import test from "node:test";

import { contentSegments } from "../src/core/message-content.js";

test("fenced ASCII diagrams become bounded code blocks", () => {
  assert.deepEqual(contentSegments("Before\n```\n |\n |\n```\nAfter"), [
    { type: "text", text: "Before\n" },
    { type: "code", language: "", text: " |\n |" },
    { type: "text", text: "\nAfter" },
  ]);
});
