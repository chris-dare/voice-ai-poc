# Voice AI

A local-first, general-purpose AI assistant with authenticated text chat, durable history, and
interruptible real-time voice. The voice gateway and Pydantic AI agent are independently deployable.

```text
Browser ── WebRTC ── voice gateway :7860 ── text/event stream ── agent API :8100
Browser ── Auth0 + JSON/SSE ── same-origin proxy ────────────────┘
                                                               ├─ durable job queue ── agent workers
                                                               ├─ model resolver
                                                               │  ├─ OpenRouter gateway (default)
                                                               │  │  ├─ OpenAI GPT-5.6 Luna (default)
                                                               │  │  └─ Anthropic Claude Sonnet
                                                               │  ├─ Ollama local models
                                                               │  └─ optional custom gateway
                                                               ├─ PostgreSQL
                                                               ├─ Monty CodeMode
                                                               ├─ deferred web/planning/specialists
                                                               └─ optional MCP servers
```

Whisper transcription, Kokoro speech, and Monty code execution run locally. OpenRouter is the
managed model gateway in this checkout, currently routing the default to OpenAI GPT-5.6 Luna while
Anthropic Claude Sonnet remains selectable. Ollama remains available as a local route, and model
selection changes through environment configuration rather than orchestration code. Web access
occurs only when the agent uses search/fetch; configured MCP servers may be external.

## Capabilities

- Model-led general conversation—no hard-coded question-to-tool routing.
- Live web search and public page retrieval.
- Sandboxed Python calculations and data transformations through Pydantic AI Harness CodeMode and
  Pydantic Monty.
- On-demand structured planning and named researcher, analyst, and reviewer sub-agents for complex
  tasks, with shared budgets and nested activity streaming.
- Optional external tools loaded from an MCP configuration file.
- Durable conversations, responses, tool activity, and resumable SSE.
- PostgreSQL-backed response dispatch with bounded admission, leases, heartbeats, retries, and
  restart recovery; API and execution workers scale independently.
- Auth0 access-token verification, subject ownership, scopes, idempotency, and rate limits.
- Responsive ChatGPT-style interface with text, voice, history, deep links, and stop generation.
- Logfire instrumentation for model calls, tool calls, HTTP, database work, system metrics, and
  application logs.
- Durable per-response token usage, model-attempt latency, OpenRouter-reported billed cost, and an
  independent estimated-cost cross-check where pricing is known.
- A versioned Pydantic Evals release gate with deterministic and explicitly opt-in live modes.

The assistant is deep-capable but stays lean by default. Search, fetch, planning, and specialist
delegation use Pydantic AI capability loading, so ordinary chat does not carry every specialist
schema and instruction. CodeMode stays eager because it is a compact, broadly useful calculation
surface. Dynamic model-authored multi-agent workflows and checkpointed long-running workflow
engines remain deliberately postponed until evaluation data justifies their complexity.
Interactive responses already use durable, leased dispatch; they recover at the response boundary
rather than claiming mid-model-call checkpoint recovery.

## Requirements

- Python 3.12
- `uv`
- Node.js 22+
- Docker
- An OpenRouter API key, or Ollama for local inference

## Setup

```sh
cp .env.example .env
docker compose up -d postgres
uv sync
uv run voice-ai doctor --fix --yes
uv run voice-ai seed
```

Choose the model host in `.env`:

```dotenv
# OpenRouter default and selectable catalogue
AGENT_MODEL=openrouter:openai/gpt-5.6-luna
AGENT_MODELS=openrouter:openai/gpt-5.6-luna,openrouter:anthropic/claude-sonnet-4.6,ollama:qwen3:1.7b
OPENROUTER_API_KEY=...
AGENT_THINKING_EFFORT=low
AGENT_DEEP_THINKING_EFFORT=medium
AGENT_PLANNING_ENABLED=false

# Optional transient-failure fallback chain
AGENT_FALLBACK_MODELS=["ollama:qwen3:1.7b"]
OLLAMA_BASE_URL=http://127.0.0.1:11434
```

OpenRouter model IDs use `openrouter:<author>/<model>`. A separately operated OpenAI Chat
Completions-compatible gateway can still be configured when required:

```dotenv
AGENT_MODEL=openai-chat:gateway-model-name
AGENT_GATEWAY_BASE_URL=https://gateway.example.com/v1
AGENT_GATEWAY_API_KEY=...
```

`AGENT_SUBAGENT_MODEL` optionally places specialists on a different host/model. If omitted, they
use the primary selection and its fallback policy. Authentication failures do not trigger fallback;
only transient API/connection failures and retryable HTTP statuses do.

Ollama is an explicit optional Compose profile and is not started for OpenRouter or another remote
gateway. For a containerized local Ollama route, start it with:

```sh
docker compose --profile local-model up -d ollama ollama-init
```

Ordinary turns default to low reasoning effort. A model-chosen delegation may use medium effort,
but each specialist has independent request, tool-call, token, wall-time, and one-call-per-role
budgets. This keeps deep work available without applying its latency and token cost to every prompt.
Structured plan tracking is separately opt-in because it adds tool turns and is unnecessary for
most interactive requests.

`voice-ai seed` applies migrations only. Domain data is never seeded into the core service.

Configure Auth0 in `.env`:

```dotenv
API_ENABLED=true
API_AGENT_ID=agent_general_assistant
AUTH0_DOMAIN=your-tenant.auth0.com
AUTH0_AUDIENCE=https://your-agent-api.example.com
AUTH0_SPA_CLIENT_ID=your-spa-client-id
AUTH0_DEFAULT_TENANT_ID=voice-ai
```

The Auth0 API permissions are:

```text
agents:invoke
conversations:write
conversations:read
conversations:delete
responses:read
responses:cancel
actions:approve
```

For the SPA, allow `http://localhost:7860` as a callback URL, logout URL, and web origin. Enable
offline access/refresh-token rotation. The browser SDK uses Authorization Code with PKCE and local
SDK persistence, so returning users normally do not log in again.

## Run

Start the complete local stack with one command:

```sh
./scripts/start.sh
```

On macOS the script starts Docker Desktop when needed; on Linux it expects the Docker daemon to be
running. It then starts PostgreSQL, builds the production frontend, applies migrations, and starts
the agent and voice gateway. It waits for both readiness endpoints and writes service output to
`.run/agent.log` and `.run/gateway.log`. Press `Ctrl+C` to stop both application services;
PostgreSQL remains available for the next start.

To use another environment file:

```sh
VOICE_AI_ENV_FILE=/path/to/environment ./scripts/start.sh
```

For a production-like multi-replica deployment, use the fail-closed
[`compose.production.yaml`](compose.production.yaml) manifest and the deployment runbook in
[`PRODUCTION_OPERATIONS.md`](docs/PRODUCTION_OPERATIONS.md). It intentionally expects externally
managed PostgreSQL, model routing, TURN, secrets, and TLS ingress instead of starting development
dependencies with default credentials.

Open <http://localhost:7860>. A conversation is directly addressable at
`/conversations/{conversation_id}`; an unauthenticated user returns there after login.

## MCP tools

Set `MCP_CONFIG_PATH` to a Pydantic AI/FastMCP-compatible configuration file. MCP servers are opened
during the agent lifespan and their tools are available through CodeMode. Keep domain credentials at
the MCP server boundary rather than embedding them in browser requests or prompts.

```dotenv
MCP_CONFIG_PATH=./mcp.json
```

Leave it blank when no MCP server is configured.

## Public API

The complete contract is [AGENT_API_SPEC.md](docs/AGENT_API_SPEC.md). A minimal conversation create is:

```sh
curl -i http://127.0.0.1:8100/v1/conversations \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: conversation-001" \
  -d '{"agent_id":"agent_general_assistant"}'
```

Browser calls go through the gateway’s same-origin `/agent-api` proxy. The browser never receives
model-provider/gateway credentials or connects directly to the model host or PostgreSQL.

The private voice seam is `POST /v1/turns/stream` with NDJSON events. It accepts `session_id`,
optional `turn_id`, and `text`; domain context belongs behind configured MCP servers.

## Quality checks

```sh
uv run ruff check .
uv run pytest -q
uv run voice-ai evals
uv run voice-ai evals --modality voice
npm --prefix frontend run check
uv run voice-ai doctor --offline
```

Both contract evals make no model or network calls. The text suite covers direct answers, tool
selection, calculations, current research, working-context recall, capability honesty,
prompt-injection resistance, source integrity, and specialist delegation. The voice contract scores
transcript accuracy, partial-transcript revision, barge-in cancellation, and latency budgets.

Run the text scenarios against the configured agent deliberately with
`uv run --env-file .env voice-ai evals --live`. Configure `AGENT_EVAL_JUDGE_MODEL` or pass
`--judge-model provider:model` to add the case-specific answer-quality judges. Live voice evaluation
is intentionally unavailable until consented recorded-audio fixtures and a hardware-profile runner
are configured; the CLI will not mislabel synthetic contract data as a live voice result. Current interactive SLOs
and metric definitions are published in [AGENT_SERVICE_SLOS.md](docs/AGENT_SERVICE_SLOS.md).
When Logfire is enabled, every CLI eval is sent there as the normal workspace for inspecting case
outputs, scores, traces, and experiment comparisons. Every run is also persisted as an immutable experiment in
PostgreSQL (`agent_eval_runs` and `agent_eval_case_results`) so CI evidence and historical results
do not depend on one observability vendor. The durable run stores the dataset digest,
model/configuration, complete report, per-case outputs and scores, timings, and Logfire trace
identifiers. Search Logfire by the `eval_run_id` printed by the CLI to correlate both views.

The frontend and API use `/live` for liveness and `/ready` for dependency readiness. The voice
gateway exposes `/health` for detailed dependency and session status.

## Dependency notes

- SQLModel defines the persisted domain models and typed async sessions; Alembic remains responsible
  for schema migrations, while SQLAlchemy primitives are retained for database-specific constraints.
- Pydantic AI is pinned to 2.14.1.
- Pydantic AI Harness is kept on compatible 0.9.x releases.
- Monty is pinned to the compatible sandbox release.
- Pydantic AI's native OpenRouter provider preserves model-specific profiles while OpenRouter owns
  upstream provider routing and credentials.
- The official package-bundled Pydantic AI skill is installed through Library Skills at
  `.agents/skills/building-pydantic-ai-agents`.

See [BRIEF.md](docs/BRIEF.md) for architecture and acceptance criteria.
