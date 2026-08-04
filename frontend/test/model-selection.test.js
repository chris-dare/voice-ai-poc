import assert from "node:assert/strict";
import test from "node:test";

import {
  modelOptionLabel,
  modelStatusLabel,
  preferredModel,
} from "../src/core/model-selection.js";

const models = [
  { id: "provider:strong", display_name: "Strong", status: "unavailable", selectable: false },
  { id: "local:quick", display_name: "Quick", status: "available", selectable: true },
  { id: "gateway:flex", display_name: "Flex", status: "unknown", selectable: true },
];

test("preferredModel skips unavailable preferences and defaults", () => {
  assert.equal(preferredModel(models, "provider:strong", "provider:strong").id, "local:quick");
  assert.equal(preferredModel(models, "gateway:flex", "local:quick").id, "gateway:flex");
  assert.equal(preferredModel([models[0]], "provider:strong", "provider:strong"), null);
});

test("model labels expose availability without provider-specific code", () => {
  assert.equal(modelOptionLabel(models[0]), "Strong — unavailable");
  assert.equal(modelStatusLabel(models[1]), "Available");
  assert.equal(modelStatusLabel(models[2]), "Ready to try");
});
