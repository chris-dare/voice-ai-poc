# Voice AI

A local-first, general-purpose AI assistant with authenticated text chat, durable history, and
interruptible real-time voice. The voice gateway and Pydantic AI agent are independently deployable.

```text
Browser ── WebRTC ── voice gateway :7860 ── text/event stream ── agent :8100
Browser ── Auth0 + JSON/SSE ── same-origin proxy ────────────────┘
                                                               ├─ model resolver
                                                               │  ├─ Anthropic (default)
                                                               │  ├─ supported Pydantic providers
                                                               │  └─ OpenAI-compatible gateway
                                                               ├─ PostgreSQL
                                                               ├─ Monty CodeMode
                                                               ├─ deferred web/planning/specialists
                                                               └─ optional MCP servers
```

Whisper transcription, Kokoro/Piper speech, and Monty code execution run locally. Model hosting is
configuration-driven: Anthropic is the default in this checkout, Ollama remains available as a
local provider, and an OpenAI-compatible gateway can be selected without changing orchestration
code. Web access occurs only when the agent uses search/fetch; configured MCP servers may be
external.

## Capabilities

- Model-led general conversation—no hard-coded question-to-tool routing.
- Live web search and public page retrieval.
- Sandboxed Python calculations and data transformations through Pydantic AI Harness CodeMode and
  Pydantic Monty.
- On-demand structured planning and named researcher, analyst, and reviewer sub-agents for complex
  tasks, with shared budgets and nested activity streaming.
- Optional external tools loaded from an MCP configuration file.
- Durable conversations, responses, tool activity, and resumable SSE.
- Auth0 access-token verification, subject ownership, scopes, idempotency, and rate limits.
- Responsive ChatGPT-style interface with text, voice, history, deep links, and stop generation.
- Logfire instrumentation for model calls, tool calls, HTTP, database work, system metrics, and
  application logs.
- Durable per-response token usage, model-attempt latency, and estimated cost where pricing is
  known.
- A versioned Pydantic Evals release gate with deterministic and explicitly opt-in live modes.

The assistant is deep-capable but stays lean by default. Search, fetch, planning, and specialist
delegation use Pydantic AI capability loading, so ordinary chat does not carry every specialist
schema and instruction. CodeMode stays eager because it is a compact, broadly useful calculation
surface. Dynamic model-authored multi-agent workflows and durable workflow engines remain
deliberately postponed until evaluation data justifies their complexity.

## Requirements

- Python 3.12
- `uv`
- Node.js 22+
- Docker
- An Anthropic API key, another supported provider credential, an OpenAI-compatible gateway, or
  Ollama for local inference

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
# Direct provider (default)
AGENT_MODEL=anthropic:claude-sonnet-4-6
ANTHROPIC_API_KEY=...

# Optional transient-failure fallback chain
AGENT_FALLBACK_MODELS=["ollama:qwen3:1.7b"]
OLLAMA_BASE_URL=http://127.0.0.1:11434
```

Any Pydantic AI `provider:model` string works when its provider extra and standard credentials are
installed. For an OpenAI Chat Completions-compatible AI gateway:

```dotenv
AGENT_MODEL=openai-chat:gateway-model-name
AGENT_GATEWAY_BASE_URL=https://gateway.example.com/v1
AGENT_GATEWAY_API_KEY=...
```

`AGENT_SUBAGENT_MODEL` optionally places specialists on a different host/model. If omitted, they
use the primary selection and its fallback policy. Authentication failures do not trigger fallback;
only transient API/connection failures and retryable HTTP statuses do.

`voice-ai seed` now applies migrations; it does not create a default subscriber or domain account.

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
optional `turn_id`, and `text`; it does not accept a subscriber ID.

## Quality checks

```sh
uv run ruff check .
uv run pytest -q
uv run voice-ai evals
npm --prefix frontend run check
uv run voice-ai doctor --offline
```

The contract eval makes no model or network calls. Run the same scenarios against the configured
agent deliberately with `uv run --env-file .env voice-ai evals --live`. Current interactive SLOs
and metric definitions are published in [AGENT_SERVICE_SLOS.md](docs/AGENT_SERVICE_SLOS.md).
When Logfire is enabled, every CLI eval is sent there as the normal workspace for inspecting case
outputs, scores, traces, and experiment comparisons. Every run is also persisted as an immutable experiment in
PostgreSQL (`agent_eval_runs` and `agent_eval_case_results`) so CI evidence and historical results
do not depend on one observability vendor. The durable run stores the dataset digest,
model/configuration, complete report, per-case outputs and scores, timings, and Logfire trace
identifiers. Search Logfire by the `eval_run_id` printed by the CLI to correlate both views.

The frontend and API use `/livez` for liveness and `/readyz` for dependency readiness. The `z`
suffix is a common convention that reduces collision with business routes; it has no special HTTP
semantics.

## Dependency notes

- Pydantic AI is pinned to 2.14.1.
- Pydantic AI Harness is kept on compatible 0.9.x releases.
- Monty is pinned to the compatible sandbox release.
- The Anthropic SDK is installed through Pydantic AI's provider extra; other provider extras can be
  added without changing `AgentRuntime`.
- The official package-bundled Pydantic AI skill is installed through Library Skills at
  `.agents/skills/building-pydantic-ai-agents`.

See [BRIEF.md](docs/BRIEF.md) for architecture and acceptance criteria.
