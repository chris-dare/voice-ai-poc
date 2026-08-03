import { REQUIRED_SCOPES, state } from "../core/state.js";
import { recordBrowserTelemetry } from "../core/telemetry.js";
import { createAuthenticatedFetch } from "./authenticated-fetch.js";

export async function getAccessToken({ cacheMode = "on" } = {}) {
  return state.auth.getTokenSilently({
    cacheMode,
    authorizationParams: {
      audience: state.config.auth.audience,
      scope: REQUIRED_SCOPES,
    },
  });
}

const authenticatedFetch = createAuthenticatedFetch({
  getToken: getAccessToken,
  getBaseUrl: () => state.config.agent_api_base,
  onAuthEvent: (event) => recordBrowserTelemetry(event),
});

export async function apiFetch(path, options = {}) {
  return authenticatedFetch(path, options);
}

export async function apiJson(path, options = {}) {
  const response = await apiFetch(path, options);
  if (!response.ok) throw await apiError(response);
  if (response.status === 204) return null;
  return response.json();
}

export async function apiError(response) {
  let data = {};
  try { data = await response.json(); } catch { /* The status is still useful. */ }
  const error = new Error(data.detail || `Request failed (${response.status}).`);
  error.status = response.status;
  error.code = data.code;
  error.data = data;
  return error;
}
