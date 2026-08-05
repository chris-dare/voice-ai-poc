# Voice AI POC — implementation brief

Build a browser-based, authenticated AI assistant with durable text conversations and
interruptible real-time voice. The assistant is general purpose first. Domain integrations,
including a future telco demo, connect later through MCP instead of being hard-coded into the
core runtime.

## Product outcome

A user can sign in, start or resume a conversation, type or speak, watch tool activity, stop a
response, and return to the same conversation URL. The assistant can answer directly, search the
live web, fetch a public page, perform calculations in a sandbox, and use configured external MCP
tools.

No runtime path maps phrases to tools with regular expressions. Pydantic AI presents capability
descriptions to the model; the model selects tools based on the request. The runtime rejects an
answer that merely promises to search, calculate, or fetch without actually doing so.

## Architecture

```text
Browser
  ├─ Auth0 Authorization Code + PKCE
  ├─ JSON + resumable SSE ───────────────┐
  └─ WebRTC audio ───────────────────┐   │
                                     ▼   │
Voice gateway :7860                      │
  ├─ static responsive chat UI           │
  ├─ same-origin /agent-api proxy ───────┤
  ├─ Faster Whisper STT                  │
  ├─ Kokoro TTS                          │
  └─ Pipecat turn-taking and barge-in    │
                                         ▼
Agent API :8100
  ├─ Auth0 JWT verification and ownership
  ├─ durable conversations/responses/events
  ├─ shared database-backed rate limits
  └─ bounded durable response queue ─────────┐
                                             ▼
Agent worker deployment
  ├─ leased claims, heartbeats, retry/recovery
  ├─ Pydantic AI agent loop
  ├─ provider-neutral model resolver
  │  ├─ OpenRouter managed gateway (default)
  │  │  ├─ OpenAI GPT-5.6 Luna (default)
  │  │  └─ Anthropic Claude Sonnet
  │  ├─ Ollama local models
  │  └─ optional custom AI gateway
  ├─ Pydantic AI Harness CodeMode
  ├─ Pydantic Monty sandbox
  ├─ deferred WebSearch, WebFetch, and Planning
  ├─ deferred researcher/analyst/reviewer sub-agents
  ├─ MCP toolsets (optional configuration)
  ├─ PostgreSQL state and ordered event log
  └─ Logfire traces, metrics, and logs
```

The voice gateway owns audio only. It never owns reasoning, tool implementations, model history,
or PostgreSQL credentials. The agent API accepts and streams work but does not own its execution;
independent workers claim durable PostgreSQL jobs. Voice, API, and worker processes load separate
configuration models and can be deployed and scaled independently.

## Agent design: deep-capable, not deep-by-default

Normal questions use the shortest useful agent path. Search, page retrieval, calculations, and a
small number of MCP calls remain interactive. CodeMode exposes one `run_code` tool backed by Monty;
inside it the model can calculate, transform data, and orchestrate multiple allowed tools without a
model round trip for every call.

Complex requests can load `Planning` and a `SubAgents` capability. The root can delegate
self-contained work to a web-enabled researcher, a Monty-enabled analyst, or an independent
reviewer, then synthesize the results. Child runs have isolated histories but share the root usage
budget. Their tool events are forwarded with agent/source/call correlation, so the UI and durable
event log show what each specialist is doing. Search, fetch, planning, and delegation are deferred;
their compact catalog is present initially and their schemas/instructions activate only when loaded.

The model layer is separate from orchestration. `AGENT_MODEL` is a Pydantic AI `provider:model`
identifier. OpenRouter is the managed model gateway and uses
`openrouter:<author>/<model>` identifiers plus one gateway credential. `openai-chat:*` can still be
pointed at a separately operated compatible gateway when required. `AGENT_SUBAGENT_MODEL` is an
optional role override. Explicit fallback models are tried only for transient model/API
failures—not invalid credentials or ordinary model behavior. Ollama is one provider option rather
than a runtime branch.

Every turn has hard limits: model requests, tool calls, total tokens, tool timeout, and agent
retries. This keeps deeper reasoning bounded. Tool calls and nested CodeMode work are traced in
Logfire. The UI shows coarse, safe lifecycle labels rather than raw arguments or results.

## Capabilities

- General conversation is handled by the environment-selected Pydantic AI model.
- `Thinking` requests configurable reasoning effort using provider-adaptive settings.
- `WebSearch` prefers provider-native search and falls back to bounded local DuckDuckGo search.
- `WebFetch` prefers provider-native fetch and falls back to a bounded local reader.
- `current_datetime` returns time in an explicit IANA timezone.
- `CodeMode` runs model-authored Python in Pydantic Monty. Monty has no ambient host filesystem,
  environment, subprocess, or network access; only explicitly exposed tools are callable.
- `Planning` exposes `write_plan` only after the model loads the planning capability.
- `SubAgents` exposes `delegate_task` only after deep work is loaded. Specialist model hosting can
  be overridden independently without changing the root agent.
- MCP toolsets load from `MCP_CONFIG_PATH`. Domain systems belong behind authenticated MCP servers,
  where their schemas, authorization, audit, approval, and reliability policies can evolve
  independently.

Pydantic AI 2.14.1 is paired with Pydantic AI Harness 0.9.x and the compatible Monty release. The
package-bundled Pydantic AI Library Skill is installed under `.agents/skills` so implementation
guidance stays synchronized with dependency upgrades.

## Conversation and API behavior

`AGENT_API_SPEC.md` is the normative public contract. The API provides:

- Auth0 RS256 access-token validation using cached JWKS, issuer, audience, expiry, and bounded
  future-`iat` validation;
- tenant and subject ownership derived only from trusted token claims/server configuration;
- scopes, per-subject rate limits, request IDs, and RFC 9457 errors;
- durable conversations and ordered response history;
- synchronous, background, and resumable SSE response creation;
- idempotent creates/actions/cancellation/deletion;
- persisted model message history and tool activity;
- cancellation and replay using `Last-Event-ID`.

Authentication establishes application identity; it does not require a telco subscriber mapping.
A future domain capability must resolve its own authorized customer/account context. Conversation
URLs are `/conversations/{conversation_id}`. An unauthenticated visitor signs in and returns to the
same URL; ownership is enforced by the agent API.

The trusted low-latency voice adapter can use the private NDJSON endpoint:

```http
POST /v1/turns/stream
Content-Type: application/json
Accept: application/x-ndjson
```

```json
{
  "session_id": "uuid",
  "turn_id": "uuid",
  "text": "Compare three approaches to reducing API latency."
}
```

Events are `response_started`, `tool_started`, `tool_completed`, `text_delta`,
`response_completed`, or `error`. Authenticated browser voice uses the durable public response API
so its transcript survives reloads.

## Voice behavior

```text
WebRTC input → Silero VAD/turn endpoint → Faster Whisper → remote agent adapter
             → Kokoro TTS → WebRTC output
```

Speech models warm after the WebRTC worker starts. The microphone stays disabled until the server
reports readiness. A new user turn cancels current model/TTS work so barge-in feels natural. The
composer remains fixed and accessible while long output scrolls, and its send button becomes a stop
control during generation.

## Operational requirements

- Python 3.12 and `uv`; Node.js builds the browser bundle.
- OpenRouter is the current managed model gateway and routes the default to OpenAI GPT-5.6 Luna;
  Anthropic Claude Sonnet remains selectable. Model and upstream provider selection change through
  environment configuration, not agent code. Ollama `qwen3:1.7b` remains a supported local
  selection.
- PostgreSQL schema changes use Alembic.
- Accepted responses and their execution jobs are committed in the same transaction. Workers use
  leases, heartbeats, bounded concurrency, retry limits, and dead-letter terminal failures.
- API rate-limit counters are shared in PostgreSQL, and SSE clients poll the durable event log at a
  bounded interval so events remain visible across replicas even without process-local signals.
- The production profile runs API and workers separately; the laptop profile may embed a worker.
- Auth0 is mandatory when the public API/UI is enabled.
- Logfire instrumentation covers FastAPI, Pydantic AI, HTTPX, SQLAlchemy/asyncpg, system metrics,
  application logs, and custom latency/auth signals.
- `/livez` answers process liveness. `/readyz` validates model configuration, locally probes Ollama
  when selected, checks PostgreSQL/auth, and avoids a billable remote model call.
- Public microphone use requires HTTPS and TURN outside localhost/LAN-safe environments.
- Kokoro is the only supported TTS backend. A session never changes voices automatically.

## Acceptance criteria

- A new authenticated user can create and resume conversations without a subscriber record.
- Text and voice turns call the real model and persist their final output.
- A calculation causes a visible `run_code` lifecycle and runs inside Monty.
- A current-information question can cause a visible web-search lifecycle.
- A complex task can load deep work, delegate to a named specialist, and expose nested tool activity.
- Switching OpenRouter models or selecting the supported local/custom gateway routes requires
  configuration only.
- Configured MCP tools become available without changing the agent’s prompt/router code.
- Tool promises without execution fail closed.
- Long output never hides the composer or stop control.
- Refreshing `/conversations/{id}` restores output and allows another turn.
- Python tests, frontend tests, lint, build, migrations, readiness, and authenticated API UAT pass.
