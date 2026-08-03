export function createAuthenticatedFetch({
  getToken,
  getBaseUrl,
  fetcher = globalThis.fetch,
  onAuthEvent = () => {},
}) {
  let refreshPromise = null;
  let tokenGeneration = 0;

  async function refreshAccessToken(observedGeneration) {
    if (observedGeneration < tokenGeneration) return getToken({ cacheMode: "on" });
    if (!refreshPromise) {
      onAuthEvent("refresh_started");
      refreshPromise = getToken({ cacheMode: "off" })
        .then((token) => {
          tokenGeneration += 1;
          onAuthEvent("refresh_succeeded");
          return token;
        })
        .catch((error) => {
          onAuthEvent("refresh_failed");
          throw error;
        })
        .finally(() => {
          refreshPromise = null;
        });
    }
    return refreshPromise;
  }

  return async function authenticatedFetch(path, options = {}) {
    const observedGeneration = tokenGeneration;
    let token;
    try {
      token = await getToken({ cacheMode: "on" });
    } catch (error) {
      if (requiresAuthentication(error)) {
        onAuthEvent("reauthentication_required");
        throw markAuthenticationRequired(error);
      }
      throw error;
    }

    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${token}`);
    if (options.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    let response = await fetcher(`${getBaseUrl()}${path}`, { ...options, headers });
    if (response.status !== 401) return response;

    try {
      token = await refreshAccessToken(observedGeneration);
    } catch (error) {
      onAuthEvent("reauthentication_required");
      throw markAuthenticationRequired(error);
    }
    headers.set("Authorization", `Bearer ${token}`);
    response = await fetcher(`${getBaseUrl()}${path}`, { ...options, headers });
    if (response.status === 401) onAuthEvent("reauthentication_required");
    return response;
  };
}

function requiresAuthentication(error) {
  return [
    "login_required",
    "consent_required",
    "missing_refresh_token",
    "invalid_grant",
  ].includes(error?.error);
}

function markAuthenticationRequired(error) {
  if (error && typeof error === "object") error.authenticationRequired = true;
  return error;
}
