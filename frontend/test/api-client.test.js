import assert from "node:assert/strict";
import test from "node:test";

import { createAuthenticatedFetch } from "../src/api/authenticated-fetch.js";

test("401 responses share one forced refresh and retry once", async () => {
  let refreshes = 0;
  const authEvents = [];
  const getToken = async (options) => {
      if (options.cacheMode === "off") {
        refreshes += 1;
        await new Promise((resolve) => setTimeout(resolve, 5));
        return "fresh-token";
      }
      return "cached-token";
  };
  const fetcher = async (_url, options) => {
    const token = options.headers.get("Authorization");
    return new Response(null, { status: token === "Bearer fresh-token" ? 200 : 401 });
  };
  const apiFetch = createAuthenticatedFetch({
    getToken,
    getBaseUrl: () => "/agent-api/v1",
    fetcher,
    onAuthEvent: (event) => authEvents.push(event),
  });
  const responses = await Promise.all([apiFetch("/conversations"), apiFetch("/responses")]);
  assert.deepEqual(responses.map((response) => response.status), [200, 200]);
  assert.equal(refreshes, 1);
  assert.deepEqual(authEvents, ["refresh_started", "refresh_succeeded"]);
});

test("a failed forced refresh is marked for user-initiated sign in", async () => {
  const authEvents = [];
  const getToken = async (options) => {
      if (options.cacheMode === "off") {
        const error = new Error("Login required");
        error.error = "login_required";
        throw error;
      }
      return "cached-token";
  };
  const apiFetch = createAuthenticatedFetch({
    getToken,
    getBaseUrl: () => "/agent-api/v1",
    fetcher: async () => new Response(null, { status: 401 }),
    onAuthEvent: (event) => authEvents.push(event),
  });
  await assert.rejects(
    apiFetch("/conversations"),
    (error) => error.authenticationRequired === true,
  );
  assert.deepEqual(authEvents, [
    "refresh_started",
    "refresh_failed",
    "reauthentication_required",
  ]);
});

test("a second 401 stops after one retry and requests explicit reauthentication", async () => {
  const authEvents = [];
  let requests = 0;
  const apiFetch = createAuthenticatedFetch({
    getToken: async ({ cacheMode }) => cacheMode === "off" ? "fresh-token" : "cached-token",
    getBaseUrl: () => "/agent-api/v1",
    fetcher: async () => {
      requests += 1;
      return new Response(null, { status: 401 });
    },
    onAuthEvent: (event) => authEvents.push(event),
  });

  const response = await apiFetch("/conversations");

  assert.equal(response.status, 401);
  assert.equal(requests, 2);
  assert.deepEqual(authEvents, [
    "refresh_started",
    "refresh_succeeded",
    "reauthentication_required",
  ]);
});
