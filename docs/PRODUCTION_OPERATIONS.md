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

`compose.production.yaml` is the fail-closed application topology for staging or a production-like
Compose host. It contains no PostgreSQL, Ollama, TURN, ingress, or default credentials: those are
external managed dependencies. Supply an immutable `VOICE_AI_IMAGE` reference and the required
variables below, validate the rendered manifest, then run migration before the independently
scalable services:

```sh
docker compose -f compose.production.yaml config --quiet
docker compose -f compose.production.yaml up migrate
docker compose -f compose.production.yaml up -d --scale agent=2 --scale agent-worker=2 --scale voice-gateway=2
```

The production manifest exposes container ports only to its Compose network. Attach a trusted TLS
ingress to the voice gateway; do not publish the private agent port directly. The default local
Compose graph no longer starts Ollama for gateway-hosted models. Start it explicitly only for a
local Ollama route with `docker compose --profile local-model up`. Container health checks use
`/livez`; ingress and rollout readiness checks must use `/readyz`, because a healthy voice replica
at its session limit deliberately becomes unready for new calls. Container process counts and
local JSON log growth are bounded by default and remain configurable by the deployment platform.

The voice gateway is stateful for each WebRTC peer connection. Route follow-up ICE requests for a
`pc_id` to the same gateway replica. `/readyz` returns 503 when that replica reaches its local
session capacity so the load balancer sends new sessions elsewhere. TURN is mandatory in the
public profile.

## Required production settings

```dotenv
AGENT_DEPLOYMENT_PROFILE=public
AGENT_RELEASE_VERSION=<immutable-image-tag-or-git-sha>
VOICE_AI_IMAGE=<immutable-image-reference-preferably-by-digest>
API_ENABLED=true
AGENT_EMBEDDED_WORKER=false
AGENT_SHARED_SECRET=<at-least-32-random-characters>
AUTH0_DOMAIN=<tenant>
AUTH0_AUDIENCE=<api-identifier>
AUTH0_SPA_CLIENT_ID=<browser-application-client-id>
DATABASE_URL=postgresql+asyncpg://...
API_MAX_REQUEST_BODY_BYTES=1048576
AGENT_QUEUE_CAPACITY=1000
AGENT_TENANT_ACTIVE_RESPONSE_LIMIT=25
AGENT_MODEL_ROUTE_CONCURRENCY=8
AGENT_TOOL_ROUTE_CONCURRENCY=8
AGENT_CAPACITY_WAIT_SECONDS=30
AGENT_CAPACITY_LEASE_SECONDS=120
AGENT_CAPACITY_HEARTBEAT_SECONDS=15
AGENT_STREAM_BUFFER_CAPACITY=128
AGENT_EVENT_BROKER_CAPACITY=10000
AGENT_MAX_OUTPUT_BYTES=1048576
LOGFIRE_ENABLED=true
LOGFIRE_ENVIRONMENT=production
LOGFIRE_CAPTURE_CONTENT=false
# To use another OTLP/HTTP backend instead, disable Logfire and set both endpoints:
# OTLP_ENDPOINT=https://collector.example/v1/traces
# OTLP_METRICS_ENDPOINT=https://collector.example/v1/metrics

DEPLOYMENT_PROFILE=public
PUBLIC_BASE_URL=https://...
ICE_SERVERS=[{"urls":"turns:...","username":"...","credential":"..."}]
VOICE_MAX_REQUEST_BODY_BYTES=1048576
```

Supply secrets through the platform secret store, not images, Compose files, or source control.
Terminate TLS at a trusted ingress, encrypt PostgreSQL connections, restrict the private turn API
to the voice service, and rotate Auth0, model-provider, gateway, MCP, TURN, and internal credentials.
The public agent profile fails readiness when `DATABASE_URL` is not a PostgreSQL `asyncpg` URL;
SQLite remains available only to isolated unit tests.
The public voice profile also refuses to start without API authentication, complete Auth0 browser
settings, HTTPS, and a TURN server. Each active WebRTC peer ID is bound to a one-way fingerprint
of the access token that created it, so renegotiation and ICE candidate requests cannot cross user
sessions and raw bearer tokens are never retained.
Both services enforce their configured raw HTTP body limit while receiving the body, including
chunked requests and clients that declare an incorrect `Content-Length`. Set the TLS ingress body
limit at or below the corresponding application limit so oversized requests are rejected before
they consume a replica's application memory.

The API `/readyz` endpoint requires at least one recently heartbeating response worker whenever the
public API is enabled. Start workers independently from API readiness (both may depend on the
completed migration job); otherwise a worker that waits for API readiness creates a startup cycle.
`/capabilities` is protected by the internal service credential and publishes the effective Core
profile and enforced execution, queue, tenant, output, and stream-buffer limits.
Every newly accepted response also persists an immutable execution snapshot. Its authenticated
owner can retrieve the credential-free projection at `/v1/responses/{response_id}/execution` to
explain the agent definition, capability set, planned model route, limits, policy version, and
observed model attempts that governed the run.

Application metrics are created through the OpenTelemetry API. Logfire is the configured backend
for this deployment, but it is not part of the metric-recording contract; setting the two OTLP
endpoints above exports traces and metrics to another compatible collector. Logfire-specific AI
views and its hosted query/dashboard experience remain optional backend features.

## Failure behavior

- Creating a response commits the response, initial event, and execution job atomically.
- A worker claims one job with a lease and renews it while the turn runs.
- If a worker loses that job lease or cannot renew it, it cancels its local model/tool execution
  immediately; lease ownership is fail-closed so two workers cannot intentionally continue the
  same response in parallel.
- If a worker dies before publishing model or tool progress, another worker may retry the turn
  after lease expiry. If progress was already published, the Core profile fails the response with
  retryable `execution_interrupted` rather than automatically replaying work. True checkpoint
  continuation remains a Durable Workflows capability.
- After the configured attempt limit, the response becomes a durable non-retryable failure rather
  than remaining stuck.
- Cancellation updates durable state first and stops a local task immediately. Remote workers see
  terminal state; tool adapters must propagate cancellation and enforce timeouts.
- SSE events are authoritative in PostgreSQL. Process-local notifications only reduce latency;
  bounded polling makes events visible when the stream and worker use different replicas.
- Queue admission is bounded. Saturation returns `503 capacity_exhausted` with `Retry-After`.
- Per-tenant active response admission is bounded independently. Tenant saturation returns
  `429 tenant_capacity_exhausted` without consuming another tenant's allocation.
- Model requests and tool calls acquire fleet-wide PostgreSQL leases by dynamic route/tool key.
  Waiting is bounded; worker loss releases capacity through lease expiry, and heartbeat failure
  cancels the owned operation rather than allowing an uncoordinated overrun.
- Runtime event buffers and process-local event/session caches are bounded; durable PostgreSQL
  state remains authoritative after eviction or worker hand-off.

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
- tenant-capacity rejection growth or sustained active-response saturation;
- elevated response failures or model-provider failures;
- PostgreSQL pool saturation, lock waits, replication lag, or storage pressure;
- Auth0/JWKS verification failures above baseline;
- voice replicas repeatedly saturated or unhealthy;
- missing Logfire/OTel telemetry from a running deployment.

Kitaru remains the preferred first evaluation for workflows that must resume inside long-running
model/tool work. Its production adoption requires a focused compatibility and crash-recovery gate;
the current Core path deliberately recovers at the response boundary. As of 2026-08-04,
`kitaru[pydantic-ai] 0.21.0` requires Pydantic AI `>=1.102,<1.104`, while this service uses Pydantic
AI `2.14.1`; dependency resolution is intentionally failed rather than downgrading the agent
harness or installing an unverified combination. Re-evaluate this gate when Kitaru publishes a
Pydantic AI v2-compatible adapter.
