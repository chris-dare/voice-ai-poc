export function recordBrowserTelemetry(event, durationMs = null) {
  const payload = { event };
  if (Number.isFinite(durationMs)) payload.duration_ms = Math.max(0, durationMs);
  void fetch("/api/telemetry", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    keepalive: true,
  }).catch(() => {});
}

export function recordNavigationInteractive() {
  const [navigation] = performance.getEntriesByType("navigation");
  if (navigation?.domInteractive) {
    recordBrowserTelemetry("navigation_interactive", navigation.domInteractive);
  }
}
