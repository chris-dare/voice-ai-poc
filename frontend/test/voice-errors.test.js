import assert from "node:assert/strict";
import test from "node:test";

import { voiceErrorPresentation } from "../src/core/voice-errors.js";

test("a pipeline error does not masquerade as a microphone failure", () => {
  assert.deepEqual(
    voiceErrorPresentation(new Error("Agent request returned 409"), true),
    {
      state: "listening",
      label: "Voice interrupted",
      detail: "I'm still listening—please try that again",
      recoverable: true,
    },
  );
});

test("a browser permission error still identifies the microphone", () => {
  const error = new Error("Permission denied");
  error.name = "NotAllowedError";
  assert.equal(voiceErrorPresentation(error, false).label, "Microphone unavailable");
});
