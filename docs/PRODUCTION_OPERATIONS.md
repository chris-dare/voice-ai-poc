# Production operations

This runbook covers the Core agent-service profile and the independently deployed voice gateway.
It does not claim the spec's Effectful Tools or Durable Workflows profiles.

## Deployment topology

```text
Internet
   │
   ▼
TLS ingress / load balancer
   ├── /, /api/offer ──► voice gateway replicas (Kokoro + Whisper, per-node capacity)
   └── /agent-api/* ───► agent API replicas (stateless request/stream handling)
                              │
                              ▼
                         PostgreSQL
                    conversations, responses,
                    events, rate limits, jobs,
                    leases and worker presence
                              ▲
                              │
                       agent worker replicas
                    Pydantic AI, models, tools,
                    MCP and sandbox execution
```

Run Alembic as a one-shot release job before starting the new API and worker revision. In the
public agent profile set `AGENT_EMBEDDED_WORKER=false`; run at least two worker replicas across
failure domains. API and workers may scale independently. Keep PostgreSQL pool budgets below the
database connection limit:

```text
total potential connections = replicas × (DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW)
```

The voice gateway is stateful for each WebRTC peer connection. Route follow-up ICE requests for a
`pc_id` to the same gateway replica. `/readyz` returns 503 when that replica reaches its local
session capacity so the load balancer sends new sessions elsewhere. TURN is mandatory in the
public profile.

## Required production settings

```dotenv
AGENT_DEPLOYMENT_PROFILE=public
API_ENABLED=true
AGENT_EMBEDDED_WORKER=false
AGENT_SHARED_SECRET=<at-least-32-random-characters>
AUTH0_DOMAIN=<tenant>
AUTH0_AUDIENCE=<api-identifier>
DATABASE_URL=postgresql+asyncpg://...
LOGFIRE_ENABLED=true
LOGFIRE_ENVIRONMENT=production
LOGFIRE_CAPTURE_CONTENT=false

DEPLOYMENT_PROFILE=public
PUBLIC_BASE_URL=https://...
ICE_SERVERS=[{"urls":"turns:...","username":"...","credential":"..."}]
```

Supply secrets through the platform secret store, not images, Compose files, or source control.
Terminate TLS at a trusted ingress, encrypt PostgreSQL connections, restrict the private turn API
to the voice service, and rotate Auth0, model-provider, gateway, MCP, TURN, and internal credentials.

## Failure behavior

- Creating a response commits the response, initial event, and execution job atomically.
- A worker claims one job with a lease and renews it while the turn runs.
- If the worker dies, another worker reclaims the job after lease expiry. A turn is therefore
  at-least-once; tools with external effects must not be enabled until they implement the spec's
  idempotency, approval, audit, and compensation requirements.
- After the configured attempt limit, the response becomes a durable non-retryable failure rather
  than remaining stuck.
- Cancellation updates durable state first and stops a local task immediately. Remote workers see
  terminal state; tool adapters must propagate cancellation and enforce timeouts.
- SSE events are authoritative in PostgreSQL. Process-local notifications only reduce latency;
  bounded polling makes events visible when the stream and worker use different replicas.
- Queue admission is bounded. Saturation returns `503 capacity_exhausted` with `Retry-After`.

## Release gate

1. Build from the locked dependency graph and generate an SBOM/image vulnerability report.
2. Apply migrations to a disposable copy and verify downgrade/forward recovery policy.
3. Run Python, frontend, concurrency, restart-recovery, authenticated API, and live evaluation
   suites.
4. Run a representative load/soak profile with the intended model route and hardware.
5. Roll out workers first, then APIs, then voice gateways; canary and observe SLO burn.
6. Roll back application revisions only when their schema compatibility window permits it.

## Alerts

Page or automatically shed load when any of these persist:

- no active worker heartbeat;
- growing `agent.worker.queue_depth` or queue-delay SLO burn;
- `execution_attempts_exhausted` responses;
- elevated response failures or model-provider failures;
- PostgreSQL pool saturation, lock waits, replication lag, or storage pressure;
- Auth0/JWKS verification failures above baseline;
- voice replicas repeatedly saturated or unhealthy;
- missing Logfire/OTel telemetry from a running deployment.

Kitaru remains the preferred first evaluation for workflows that must resume inside long-running
model/tool work. Its production adoption requires a focused compatibility and crash-recovery gate;
the current Core path deliberately recovers at the response boundary.
