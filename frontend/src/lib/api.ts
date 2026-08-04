import type { Auth0Client } from "@auth0/auth0-spa-js";
import { createAuthenticatedFetch } from "../api/authenticated-fetch.js";
import { recordBrowserTelemetry } from "../core/telemetry.js";
import type { AppConfig } from "../types";

export const REQUIRED_SCOPES = [
  "openid",
  "profile",
  "email",
  "agents:invoke",
  "conversations:write",
  "conversations:read",
  "conversations:delete",
  "responses:read",
  "responses:cancel",
  "actions:approve",
].join(" ");

export class ApiError extends Error {
  status?: number;
  code?: string;
  data?: Record<string, unknown>;
  authenticationRequired?: boolean;
}

export interface ApiClient {
  fetch(path: string, options?: RequestInit): Promise<Response>;
  json<T>(path: string, options?: RequestInit): Promise<T>;
  accessToken(options?: { cacheMode?: "on" | "off" }): Promise<string>;
}

export function createApiClient(auth: Auth0Client, config: AppConfig): ApiClient {
  const accessToken = ({ cacheMode = "on" }: { cacheMode?: "on" | "off" } = {}) => (
    auth.getTokenSilently({
      cacheMode,
      authorizationParams: {
        audience: config.auth?.audience,
        scope: REQUIRED_SCOPES,
      },
    })
  );

  const buildAuthenticatedFetch = createAuthenticatedFetch as unknown as (options: {
    getToken: typeof accessToken;
    getBaseUrl: () => string;
    onAuthEvent: (event: string) => void;
  }) => (path: string, options?: RequestInit) => Promise<Response>;

  const authenticatedFetch = buildAuthenticatedFetch({
    getToken: accessToken,
    getBaseUrl: () => config.agent_api_base,
    onAuthEvent: (event: string) => recordBrowserTelemetry(event),
  });

  return {
    accessToken,
    fetch: authenticatedFetch,
    async json<T>(path: string, options: RequestInit = {}): Promise<T> {
      const response = await authenticatedFetch(path, options);
      if (!response.ok) throw await apiError(response);
      if (response.status === 204) return null as T;
      return response.json() as Promise<T>;
    },
  };
}

export async function apiError(response: Response): Promise<ApiError> {
  let data: Record<string, unknown> = {};
  try { data = await response.json() as Record<string, unknown>; } catch { /* status is enough */ }
  const error = new ApiError(String(data.detail || `Request failed (${response.status}).`));
  error.status = response.status;
  error.code = typeof data.code === "string" ? data.code : undefined;
  error.data = data;
  return error;
}
