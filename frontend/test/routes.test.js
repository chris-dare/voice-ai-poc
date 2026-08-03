import assert from "node:assert/strict";
import test from "node:test";

import { conversationIdFromPath, conversationPath, safeReturnTo } from "../src/core/routes.js";

test("conversation routes round trip", () => {
  const id = "conv_c35053a9c86649eba8f302527fd33574";
  assert.equal(conversationPath(id), `/conversations/${id}`);
  assert.equal(conversationIdFromPath(`/conversations/${id}`), id);
  assert.equal(conversationIdFromPath("/"), null);
});

test("Auth0 return paths cannot become external redirects", () => {
  assert.equal(safeReturnTo("/conversations/conv_123"), "/conversations/conv_123");
  assert.equal(safeReturnTo("//evil.example"), "/");
  assert.equal(safeReturnTo("https://evil.example"), "/");
});
