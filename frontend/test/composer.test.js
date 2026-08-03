import assert from "node:assert/strict";
import test from "node:test";

import { promptAction } from "../src/core/composer.js";

test("empty submit during generation stops the current response", () => {
  assert.deepEqual(promptAction(true, "  "), { type: "stop", text: "" });
});

test("typed submit during generation interrupts and sends the new prompt", () => {
  assert.deepEqual(promptAction(true, "  wait, summarize that  "), {
    type: "interrupt",
    text: "wait, summarize that",
  });
});

test("normal composer ignores blanks and sends text", () => {
  assert.deepEqual(promptAction(false, " "), { type: "ignore", text: "" });
  assert.deepEqual(promptAction(false, "hello"), { type: "send", text: "hello" });
});
