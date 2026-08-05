# Public AI Agent API Specification

**Status:** Draft  
**API version:** `v1`  
**Style:** Resource-oriented HTTPS API with JSON representations and optional
Server-Sent Events (SSE) streaming

This document defines a public API for invoking an AI agent. It describes the
external contract only; prompts, tools, memory implementations, model routing, and
orchestration frameworks are private implementation details.

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are to
be interpreted as normative requirements.

## 1. Design principles

1. The API represents agent work as a durable `response` resource.
2. A client MAY receive a completed response as JSON or consume incremental events
   over SSE.
3. Every response has an explicit lifecycle and an unambiguous terminal state.
4. Conversations are first-class resources owned by the agent service.
5. Authentication determines tenant and user scope. Clients MUST NOT be allowed to
   select an arbitrary user or tenant merely by changing a request-body identifier.
6. Tools execute inside the agent service by default. Clients see safe lifecycle
   information, not private prompts, credentials, tool arguments, or raw records.
7. The public contract does not expose private orchestration implementation details.
8. Additive changes are preferred. Clients MUST ignore unknown JSON properties and
   unknown event types.

## 2. Protocol conventions

### 2.1 Base URL and versioning

All endpoints are rooted at:

```text
https://api.example.com/v1
```

Breaking changes require a new major path such as `/v2`. New optional fields,
resources, event types, and enum values MAY be added to `v1`.

### 2.2 Transport security

Production traffic MUST use HTTPS with TLS 1.2 or later. Plain HTTP MAY be used on
loopback interfaces during local development.

### 2.3 Authentication and authorization

Requests use a bearer access token:

```http
Authorization: Bearer <access-token>
```

The service SHOULD support OAuth 2.0 access tokens for delegated access and MAY
support scoped API keys for server-to-server integrations.

Tokens SHOULD carry or resolve to:

- tenant or organization identity;
- authenticated subject identity;
- permitted agent identifiers;
- authorization scopes;
- expiry and revocation state.

Example scopes:

```text
agents:invoke
conversations:write
conversations:read
conversations:delete
responses:read
responses:cancel
actions:approve
```

Credentials MUST NOT be accepted in URLs or query parameters.

### 2.4 Media types

| Purpose | Media type |
|---|---|
| JSON request or response | `application/json` |
| Streaming response | `text/event-stream` |
| HTTP error | `application/problem+json` |

UTF-8 MUST be used.

### 2.5 Identifiers and timestamps

Identifiers are opaque, case-sensitive strings. Clients MUST NOT parse them or
infer ordering from them.

Recommended prefixes:

```text
agent_...
conv_...
resp_...
act_...
req_...
```

Timestamps MUST use RFC 3339 UTC:

```text
2026-07-30T14:25:31.482Z
```

### 2.6 Request correlation

The server MUST return a request identifier on every response:

```http
X-Request-Id: req_01J...
```

A client MAY provide its own `X-Request-Id`. The server MAY retain it or generate a
new identifier, but MUST return the effective value.

### 2.7 Idempotency

Clients SHOULD send an idempotency key when creating responses, submitting actions,
or requesting cancellation:

```http
Idempotency-Key: 0d70b5d1-446d-49aa-8bb8-3f31df2743b7
```

The key is scoped to the authenticated tenant and subject, HTTP method, and route.
The server MUST compare request fingerprints after parsing JSON, applying API
defaults, and canonicalizing object-key order. Array order remains significant.

Repeating a request with the same key and the same fingerprint MUST refer to the
original operation:

- if the operation is complete, return its original result;
- if response creation is still active and JSON was requested, return `202
  Accepted` with the existing response object and its ID;
- if response creation is still active and SSE was requested, replay the retained
  events and then follow the existing response live;
- concurrent duplicates MUST NOT start duplicate agent or tool execution.

The server SHOULD return `Idempotent-Replayed: true` when a request is served from
an existing operation. Reusing a key with a different fingerprint MUST return `409
Conflict` with error code `idempotency_conflict`.

If a response-creation connection is lost before the client learns the response
ID, the client recovers by repeating the same request with the same idempotency
key. This is the required response-discovery mechanism for that failure mode.

The server SHOULD retain idempotency records for at least 24 hours.

## 3. Resource model

```text
Agent
  └── Conversation
        ├── Response
        │     ├── output items
        │     ├── events
        │     └── required actions
        └── Response
```

### 3.1 Agent

An agent is a server-controlled configuration containing its instructions,
capabilities, safety policy, tools, and model selection.

Clients select an agent by ID. They do not submit arbitrary system prompts or
replace its tools unless explicitly authorized by a separate administrative API.

### 3.2 Conversation

A conversation owns multi-turn context. The service decides how history is stored,
retrieved, summarized, or compacted.

```json
{
  "id": "conv_01J...",
  "object": "conversation",
  "agent_id": "agent_general_assistant",
  "model": "provider:model-id",
  "status": "active",
  "created_at": "2026-07-30T14:20:00Z",
  "updated_at": "2026-07-30T14:25:31Z",
  "metadata": {
    "channel": "voice"
  }
}
```

Conversation statuses:

- `active`
- `deleted`

Conversation deletion is synchronous in `v1`. The service's published data
retention policy MUST identify any legally required retention that overrides
physical deletion. Deleting a conversation with an active response MUST return
`409 Conflict` with error code `conversation_busy`; the client must cancel that
response first. Once deleted, reads return `404 Not Found`; repeating the same
authorized deletion remains successful with `204 No Content`.

### 3.3 Response

A response represents one agent execution.

```json
{
  "id": "resp_01J...",
  "object": "response",
  "agent_id": "agent_general_assistant",
  "model": "provider:model-id",
  "conversation_id": "conv_01J...",
  "status": "completed",
  "created_at": "2026-07-30T14:25:30Z",
  "started_at": "2026-07-30T14:25:30.120Z",
  "completed_at": "2026-07-30T14:25:31.482Z",
  "cancellation_reason": null,
  "input": [
    {
      "type": "message",
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "What is the latest Pydantic AI release?"
        }
      ]
    }
  ],
  "output": [
    {
      "id": "msg_01J...",
      "type": "message",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "The latest Pydantic AI release is 2.14.1."
        }
      ]
    }
  ],
  "required_action": null,
  "error": null,
  "usage": {
    "input_tokens": 84,
    "output_tokens": 12,
    "total_tokens": 96,
    "reasoning_tokens": 18,
    "model_requests": 1,
    "tool_calls": 2,
    "wall_clock_ms": 1482.0,
    "model_duration_ms": 1210.4,
    "route_model": "openrouter:openai/gpt-5.6-luna",
    "route_provider": "openrouter",
    "gateway": true,
    "fallback_models": [],
    "actual_models": ["openai/gpt-5.6-luna"],
    "reported_cost_usd": "0.000401000000",
    "estimated_cost_usd": "0.000432000000",
    "cost_currency": "USD",
    "cost_status": "reported",
    "cost_source": "openrouter",
    "cost_source_version": null,
    "latency": {
      "queue_delay_ms": 18.0,
      "time_to_first_text_ms": 1401.2,
      "completion_ms": 1464.0
    }
  },
  "metadata": {
    "channel": "voice"
  }
}
```

Response statuses:

- `queued`
- `in_progress`
- `requires_action`
- `completed`
- `failed`
- `cancelled`

Terminal statuses are `completed`, `failed`, and `cancelled`.

`started_at`, `completed_at`, and `usage` MAY be `null` while work is active.
Token values are provider-reported where available. Cost MAY be provider-reported, estimated,
partial, or unavailable and MUST identify its currency, status, and source when present. A
per-attempt `provider_response_id` MAY be returned for support and billing reconciliation; clients
MUST treat it as opaque. Clients MUST tolerate additional usage detail, including per-model-attempt
and SLO fields, without treating the public response itself as billing authority.
`error` MUST be non-null only for a failed response. `cancellation_reason` MUST be
non-null only for a cancelled response; known values are `client_request`,
`connection_lost`, `action_expired`, and `server_request`. Clients MUST tolerate
new reason values.

An acknowledgement such as “I will check” MUST NOT cause a response to become
`completed` when tool work or another operation is still outstanding.

At most one response in `queued`, `in_progress`, or `requires_action` status may
exist for a conversation. A second creation attempt MUST return `409 Conflict` with
error code `conversation_busy` and identify the existing response. This invariant
serializes conversation history and tool side effects.

## 4. Endpoints

### 4.0 List available models

```http
GET /v1/models
Authorization: Bearer <access-token>
```

Returns the deployment's configured model catalogue and the service's latest safe availability
assessment. Clients MUST treat model IDs as opaque strings and MUST NOT maintain their own fixed
list. A model with `selectable: false` MUST NOT be submitted for a new response.

```json
{
  "object": "list",
  "default": "provider:model-id",
  "data": [
    {
      "id": "provider:model-id",
      "object": "model",
      "display_name": "Model Id",
      "provider": "provider",
      "status": "available",
      "available": true,
      "selectable": true,
      "default": true,
      "reason_code": null,
      "detail": "Ready",
      "checked_at": "2026-08-03T12:00:00Z"
    }
  ]
}
```

Availability is advisory and can change between catalogue retrieval and invocation. The response
creation endpoint remains authoritative and returns `model_not_allowed` or `model_unavailable`
when the selected route cannot be accepted.

### 4.1 Create a conversation

```http
POST /v1/conversations
Authorization: Bearer <access-token>
Content-Type: application/json
Idempotency-Key: <key>
```

```json
{
  "agent_id": "agent_general_assistant",
  "model": "provider:model-id",
  "metadata": {
    "channel": "voice"
  }
}
```

Successful response:

```http
HTTP/1.1 201 Created
Location: /v1/conversations/conv_01J...
Content-Type: application/json
```

The response body is a conversation object.

### 4.2 List conversations

```http
GET /v1/conversations?limit=30&after=<cursor>
Authorization: Bearer <access-token>
```

Returns the caller's active conversations in descending `updated_at` order. The
default `limit` is 30 and the maximum is 100. `after` is the opaque
`next_cursor` from the previous page.

```json
{
  "object": "list",
  "data": [],
  "has_more": false,
  "next_cursor": null
}
```

### 4.3 Retrieve a conversation

```http
GET /v1/conversations/{conversation_id}
Authorization: Bearer <access-token>
```

Returns `200 OK` with the conversation object.

### 4.4 Delete a conversation

```http
DELETE /v1/conversations/{conversation_id}
Authorization: Bearer <access-token>
Idempotency-Key: <key>
```

Returns:

- `204 No Content` when deletion is complete.

Deletion MUST be idempotent.

### 4.5 Create a response

```http
POST /v1/responses
Authorization: Bearer <access-token>
Content-Type: application/json
Accept: application/json
Idempotency-Key: <key>
```

```json
{
  "agent_id": "agent_general_assistant",
  "conversation_id": "conv_01J...",
  "model": "provider:model-id",
  "input": [
    {
      "type": "message",
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "What is the latest Pydantic AI release?"
        }
      ]
    }
  ],
  "stream": false,
  "background": false,
  "metadata": {
    "channel": "voice",
    "client_turn_id": "turn_01J..."
  }
}
```

Fields:

| Field | Required | Description |
|---|---:|---|
| `agent_id` | Conditional | Required without `conversation_id`; otherwise it MUST be omitted or match the conversation's agent |
| `conversation_id` | No | Existing conversation; omitted for an isolated turn |
| `model` | Yes | Opaque model ID obtained from `GET /v1/models`; selects this response's execution route |
| `input` | Yes | Ordered input items |
| `stream` | No | Return SSE when `true`; default `false` |
| `background` | No | Return immediately and continue asynchronously; default `false` |
| `metadata` | No | Client-defined, non-sensitive correlation values |

When `conversation_id` is present, the conversation's `agent_id` is authoritative.
A conflicting request value MUST return `409 Conflict` with error code
`agent_mismatch`.

The response request's `model` is authoritative for that execution. The service MUST persist it on
the response before queueing work, and every retry or background worker attempt MUST use the same
snapshot. A conversation MAY remember the most recently selected model to restore client UI state,
but that mutable preference MUST NOT replace the required per-response field. Consequently, one
conversation may contain responses produced by different models with an accurate per-response
audit trail.

For a synchronous non-streaming request, return `200 OK` with the completed,
failed, cancelled, or requires-action response object.

For a non-streaming background request, return:

```http
HTTP/1.1 202 Accepted
Location: /v1/responses/resp_01J...
Content-Type: application/json
```

The body contains the current response object with status `queued` or
`in_progress`.

`background: true` MAY be combined with `stream: true`. The server immediately
streams the background response, and execution continues if that connection
disconnects. When `background` is `false`, losing the request connection cancels
the in-flight synchronous execution on a best-effort basis.

Every successful response-creation HTTP response SHOULD include:

```http
X-Response-Id: resp_01J...
```

The response body or initial `response.created` event also contains the response
ID. If neither reaches the client, the client recovers using the original
`Idempotency-Key` as defined in Section 2.7.

#### Simplified text input

The API MAY accept a string as shorthand:

```json
{
  "agent_id": "agent_general_assistant",
  "conversation_id": "conv_01J...",
  "model": "provider:model-id",
  "input": "What is the latest Pydantic AI release?"
}
```

It is equivalent to one user message containing one `input_text` item.

### 4.6 Stream a response

The request-body field is authoritative:

```json
{
  "stream": true
}
```

`Accept: text/event-stream` is optional. If `Accept` is present and explicitly
excludes `text/event-stream`, the server MUST return `406 Not Acceptable`. When
`stream` is false, the server returns JSON; an `Accept` header that explicitly
excludes `application/json` likewise produces `406`.

The server returns:

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
```

`X-Accel-Buffering` is an optional deployment hint for compatible reverse proxies.
Deployments MUST otherwise ensure that proxy or application buffering does not
delay events.

Browser clients MUST consume POST and authenticated GET streams with `fetch()` and
an SSE parser. Native `EventSource` is not sufficient because it cannot send the
required `Authorization` header or make the streaming POST request.

### 4.7 Retrieve a response

```http
GET /v1/responses/{response_id}
Authorization: Bearer <access-token>
```

Returns the latest durable response representation. Clients SHOULD use this
endpoint to recover after a dropped stream.

### 4.8 List conversation responses

```http
GET /v1/conversations/{conversation_id}/responses?limit=100&after=<cursor>
Authorization: Bearer <access-token>
```

Returns the conversation's responses in ascending `created_at` order so clients
can reconstruct the transcript from each response's `input` and `output`. The
default and maximum `limit` are 100. This endpoint requires both
`conversations:read` and `responses:read`.

### 4.9 Observe or resume response events

```http
GET /v1/responses/{response_id}/events
Authorization: Bearer <access-token>
Accept: text/event-stream
Last-Event-ID: 17
```

`Last-Event-ID` is the decimal `sequence_number` of the last event fully processed
by the client. The server MUST replay every retained event after that sequence and
then follow the response live. Replayed and live events share one continuous
sequence; no boundary marker is necessary.

If `Last-Event-ID` is absent, the server MUST replay from sequence 1 and then
follow live events. A terminal response is replayed through its terminal event and
the stream then closes.

Events MUST remain available for at least 24 hours after a response becomes
terminal. If the requested cursor is invalid or no longer retained, return `409
Conflict` with error code `event_cursor_expired`. The client then retrieves the
durable result with `GET /v1/responses/{id}`.

### 4.10 Cancel a response

```http
POST /v1/responses/{response_id}/cancel
Authorization: Bearer <access-token>
Idempotency-Key: <key>
```

Returns `200 OK` with the latest response object. Cancellation is best-effort:

- a `queued`, `in_progress`, or `requires_action` response remains in its current
  state until cancellation commits, then becomes `cancelled`;
- a terminal response remains unchanged;
- cancellation MUST be idempotent.

Disconnecting a background stream does not cancel its response. Clients that
require background cancellation MUST call this endpoint. A disconnected
non-background request is cancelled on a best-effort basis.

### 4.11 Submit a required action

When a response requires explicit approval, it has status `requires_action`:

```json
{
  "status": "requires_action",
  "required_action": {
    "id": "act_01J...",
    "type": "confirmation",
    "title": "Change plan to Fibre Max",
    "description": "The new monthly charge will be GHS 350.00.",
    "expires_at": "2026-07-30T15:30:00Z"
  }
}
```

The client submits a decision:

```http
POST /v1/responses/{response_id}/actions/{action_id}
Authorization: Bearer <access-token>
Content-Type: application/json
Idempotency-Key: <key>
```

```json
{
  "decision": "approve"
}
```

Supported confirmation decisions:

- `approve`
- `reject`

The server validates that the action belongs to the authenticated subject, has not
expired, and has not already received a conflicting decision.

- `approve` resumes execution of the same response;
- `reject` resumes execution without performing the proposed side effect and
  normally completes with a clear statement that no action was taken;
- an unanswered action that reaches `expires_at` transitions the response to
  `cancelled` with reason `action_expired` and emits `response.cancelled`;
- a conflicting repeated decision returns `409 Conflict` with error code
  `action_already_resolved`.

The endpoint returns `200 OK` with the latest response representation. If the
response is still active, the client can observe it through the response event
endpoint.

Sensitive or irreversible actions SHOULD require a fresh authorization check and
MAY require step-up authentication.

## 5. Server-Sent Events protocol

### 5.1 Event framing

Each event is an SSE record terminated by a blank line:

```text
id: 4
event: response.output_text.delta
data: {"type":"response.output_text.delta","sequence_number":4,"response_id":"resp_01J...","created_at":"2026-07-30T14:25:31.120Z","delta":"GHS 42.50."}

```

The `data` value MUST be one complete, compact JSON object serialized on a single
`data:` line. Newline characters inside JSON strings MUST be escaped by the JSON
serializer.

Network packets and HTTP chunks do not define event boundaries. Clients MUST use
an SSE parser rather than assuming one read equals one event.

### 5.2 Common event fields

Every event data object contains:

```json
{
  "type": "response.output_text.delta",
  "sequence_number": 4,
  "response_id": "resp_01J...",
  "created_at": "2026-07-30T14:25:31.120Z"
}
```

Requirements:

- `type` MUST match the SSE `event:` value.
- `sequence_number` MUST start at 1 and increase by exactly 1 for every retained
  event within a response.
- the SSE `id:` MUST equal the decimal `sequence_number`;
- events for one response MUST be emitted in sequence order.
- the pair `(response_id, sequence_number)` uniquely identifies an event for
  deduplication and replay.

### 5.3 Event types

#### `response.created`

Emitted once after the response resource is created.

```text
id: 1
event: response.created
data: {"type":"response.created","sequence_number":1,"response_id":"resp_01","created_at":"2026-07-30T14:25:30Z","response":{"id":"resp_01","object":"response","agent_id":"agent_general_assistant","conversation_id":"conv_01J...","status":"queued","created_at":"2026-07-30T14:25:30Z","started_at":null,"completed_at":null,"cancellation_reason":null,"output":[],"required_action":null,"error":null,"usage":null,"metadata":{}}}

```

#### `response.in_progress`

Indicates that agent execution has started.

#### `response.output_text.delta`

Carries an incremental text fragment:

```text
id: 3
event: response.output_text.delta
data: {"type":"response.output_text.delta","sequence_number":3,"response_id":"resp_01","created_at":"2026-07-30T14:25:31Z","item_id":"msg_01","content_index":0,"delta":"The latest Pydantic AI release is "}

```

Clients concatenate deltas in `sequence_number` order for the same `item_id` and
`content_index`. A delta MAY contain any Unicode text, including whitespace.

#### `response.tool.started`

Provides safe progress information:

```text
id: 2
event: response.tool.started
data: {"type":"response.tool.started","sequence_number":2,"response_id":"resp_01","created_at":"2026-07-30T14:25:30.500Z","tool_run_id":"toolrun_01","name":"duckduckgo_search","label":"Searching the web"}

```

The event MUST NOT expose credentials, unrestricted tool arguments, raw database
records, or private chain-of-thought.

#### `response.tool.completed`

Indicates completion of a server-managed tool:

```text
id: 3
event: response.tool.completed
data: {"type":"response.tool.completed","sequence_number":3,"response_id":"resp_01","created_at":"2026-07-30T14:25:30.900Z","tool_run_id":"toolrun_01","name":"duckduckgo_search","status":"succeeded","label":"Web search completed"}

```

Tool status is `succeeded` or `failed`. A failed tool does not necessarily mean the
entire response fails; the agent MAY recover or explain the limitation.

#### `response.requires_action`

Contains the response ID and required-action object. The server MUST close the
current stream after this event so that approval does not hold an HTTP connection
open indefinitely. The client can submit the action and reconnect to the response
event endpoint.

#### `response.completed`

Contains the complete durable response object. This is the successful terminal
event.

#### `response.failed`

Contains the complete durable failed response. Its `error` object is safe for the
caller:

```text
id: 8
event: response.failed
data: {"type":"response.failed","sequence_number":8,"response_id":"resp_01","created_at":"2026-07-30T14:25:32Z","response":{"id":"resp_01","object":"response","agent_id":"agent_general_assistant","conversation_id":"conv_01J...","status":"failed","created_at":"2026-07-30T14:25:30Z","started_at":"2026-07-30T14:25:30.100Z","completed_at":"2026-07-30T14:25:32Z","cancellation_reason":null,"output":[],"required_action":null,"error":{"code":"dependency_unavailable","message":"A required dependency is temporarily unavailable.","retryable":true},"usage":{"input_tokens":84,"output_tokens":0,"total_tokens":84},"metadata":{}}}

```

#### `response.cancelled`

Contains the complete durable cancelled response object. This is a terminal event.

### 5.4 Stream termination

A stream MUST end after exactly one of:

- `response.completed`;
- `response.failed`;
- `response.cancelled`; or
- `response.requires_action`, which pauses rather than terminates the response
  resource.

The API MUST NOT use an untyped sentinel such as `[DONE]`.

### 5.5 Heartbeats

For streams that may be idle, the server SHOULD send an SSE comment every 15–30
seconds:

```text
: ping

```

Heartbeats are SSE comments rather than events. They do not have an `id`, do not
increment `sequence_number`, and are not retained for replay.

### 5.6 Backpressure and slow consumers

The server MUST apply bounded buffering. If a client remains too slow, the server
MAY close the stream. A background response continues; cancellation of a
non-background response remains best-effort as defined in Section 4.5. The client
recovers through the response or event retrieval endpoints.

## 6. Input and output items

### 6.1 Text input

```json
{
  "type": "message",
  "role": "user",
  "content": [
    {
      "type": "input_text",
      "text": "What is the latest Pydantic AI release?"
    }
  ]
}
```

### 6.2 Text output

```json
{
  "id": "msg_01J...",
  "type": "message",
  "role": "assistant",
  "content": [
    {
      "type": "output_text",
      "text": "The latest Pydantic AI release is 2.14.1."
    }
  ]
}
```

Additional content types MAY be added later. Clients MUST ignore unknown output
items while preserving their relative ordering.

### 6.3 Tool activity output

Completed responses MAY retain compact tool-activity items so clients can restore
what the agent did without replaying every text-delta event:

```json
{
  "id": "toolrun_01J...",
  "type": "tool_activity",
  "name": "explain_latest_bill",
  "label": "Analysing your latest bill",
  "status": "succeeded"
}
```

Live clients SHOULD continue to use `response.tool.started` and
`response.tool.completed` for immediate progress. The retained output item is a
terminal summary and MUST NOT contain private tool arguments or raw results.

## 7. Errors

### 7.1 Errors before streaming starts

Errors use RFC 9457 Problem Details:

```http
HTTP/1.1 422 Unprocessable Content
Content-Type: application/problem+json
X-Request-Id: req_01J...
```

```json
{
  "type": "https://api.example.com/problems/invalid-request",
  "title": "Invalid request",
  "status": 422,
  "detail": "The input field must contain at least one item.",
  "instance": "/v1/responses",
  "code": "invalid_input",
  "request_id": "req_01J...",
  "errors": [
    {
      "path": "$.input",
      "code": "min_items",
      "message": "At least one input item is required."
    }
  ]
}
```

### 7.2 Errors after streaming starts

After the server has sent `200 OK`, it cannot change the HTTP status. It MUST emit
a `response.failed` event for a fatal execution error. A malformed connection that
prevents event delivery is recoverable through `GET /v1/responses/{id}`.

### 7.3 HTTP status codes

| Status | Meaning |
|---:|---|
| `200` | Successful retrieval, completion, cancellation, or stream |
| `201` | Resource created |
| `202` | Accepted for background processing or an idempotent JSON replay of active work |
| `204` | Successful operation with no body |
| `400` | Malformed request |
| `401` | Missing or invalid authentication |
| `403` | Authenticated but not authorized |
| `404` | Resource not found or not visible to the caller |
| `406` | Requested representation is not acceptable |
| `409` | State conflict, expired event cursor, or idempotency conflict |
| `413` | Request body exceeds the documented size limit |
| `415` | Unsupported request media type |
| `422` | Syntactically valid request with invalid fields |
| `429` | Rate limit exceeded |
| `500` | Unexpected server failure |
| `502` | Invalid or failed upstream dependency |
| `503` | Temporarily unavailable |
| `504` | Upstream timeout |

The service SHOULD return `Retry-After` for `429` and retryable `503` responses.

## 8. Pagination

List endpoints MUST use opaque cursor pagination:

```http
GET /v1/conversations?limit=20&after=cursor_01J...
```

```json
{
  "object": "list",
  "data": [],
  "has_more": false,
  "next_cursor": null
}
```

The default and maximum `limit` values MUST be documented. Offset pagination
SHOULD NOT be used for mutable collections.

## 9. Rate limiting

The server SHOULD return `RateLimit-Limit`, `RateLimit-Remaining`, and
`RateLimit-Reset` headers. It MUST return `Retry-After` when rejecting a request
with `429`. If multiple limit policies apply, the server MUST document how the
reported values are selected.

Limits MAY be applied by tenant, subject, agent, endpoint, token usage, concurrent
response count, or tool cost. Error details MUST NOT reveal another tenant's usage.

## 10. Security and privacy

The implementation MUST:

- derive tenant and subject authorization from trusted credentials;
- verify resource ownership on every read, stream, action, and cancellation;
- apply least privilege to tools and downstream services;
- treat retrieved web content and tool output as untrusted input;
- redact secrets, tokens, and sensitive personal data from logs;
- avoid returning private prompts or chain-of-thought;
- bound input size, execution time, tool calls, output size, and concurrency;
- validate all tool arguments before execution;
- require explicit approval for sensitive side effects;
- maintain an audit trail for tool calls and approved actions;
- document data retention and deletion behavior.

Metadata MUST NOT be used as an authorization boundary. The service SHOULD limit
metadata key count, key length, value length, and total encoded size.

## 11. Reliability and observability

The server SHOULD record:

- request and response IDs;
- authenticated tenant and subject identifiers in privacy-safe form;
- agent ID and conversation ID;
- lifecycle timestamps;
- first-event and first-text latency;
- total latency;
- tool duration and outcome;
- token or compute usage;
- terminal status and safe error code.

Distributed tracing MAY use W3C `traceparent` and `tracestate` headers.

The API SHOULD expose separate liveness and readiness endpoints to deployment
infrastructure. These operational endpoints are not part of the public agent
contract and SHOULD require appropriate network controls.

## 12. Design influences

This API adopts established patterns that are useful for agent applications,
including response resources, typed output items, conversation state, SSE lifecycle
events, background execution, cancellation, and resumable streams.

Similar names or structures do not imply wire-level or SDK compatibility with any
other API. This specification may deliberately differ where the product requires
server-managed tools, explicit approvals, stronger recovery behavior, or different
security and identity semantics.

External documentation MUST describe the implemented contract directly rather than
claiming compatibility based on resemblance. Any future compatibility adapter
SHOULD be a separately documented surface with its own contract tests and
deprecation policy.

## 13. WebSocket and realtime media

WebSocket is intentionally not required for `v1`. SSE is appropriate when a client
sends a complete request and the server streams progress and output in one
direction.

A future WebSocket API is justified only when clients need to send incremental
input, tool results, or control events while the same response is running.

Realtime microphone and speaker media SHOULD use a media-oriented protocol such as
WebRTC and remain separate from this text-and-events agent API.

## 14. End-to-end streaming example

Request:

```http
POST /v1/responses HTTP/1.1
Host: api.example.com
Authorization: Bearer <access-token>
Content-Type: application/json
Accept: text/event-stream
Idempotency-Key: c9ef6c5f-79ad-4bca-83ce-55f433ef34cd
```

```json
{
  "agent_id": "agent_general_assistant",
  "conversation_id": "conv_01J...",
  "model": "provider:model-id",
  "input": "What is the latest Pydantic AI release?",
  "stream": true
}
```

Response:

```text
id: 1
event: response.created
data: {"type":"response.created","sequence_number":1,"response_id":"resp_01","created_at":"2026-07-30T14:25:30Z","response":{"id":"resp_01","object":"response","agent_id":"agent_general_assistant","conversation_id":"conv_01J...","status":"queued","created_at":"2026-07-30T14:25:30Z","started_at":null,"completed_at":null,"cancellation_reason":null,"output":[],"required_action":null,"error":null,"usage":null,"metadata":{}}}

id: 2
event: response.in_progress
data: {"type":"response.in_progress","sequence_number":2,"response_id":"resp_01","created_at":"2026-07-30T14:25:30.100Z"}

id: 3
event: response.tool.started
data: {"type":"response.tool.started","sequence_number":3,"response_id":"resp_01","created_at":"2026-07-30T14:25:30.300Z","tool_run_id":"toolrun_01","name":"duckduckgo_search","label":"Searching the web"}

id: 4
event: response.tool.completed
data: {"type":"response.tool.completed","sequence_number":4,"response_id":"resp_01","created_at":"2026-07-30T14:25:30.800Z","tool_run_id":"toolrun_01","name":"duckduckgo_search","status":"succeeded","label":"Web search completed"}

id: 5
event: response.output_text.delta
data: {"type":"response.output_text.delta","sequence_number":5,"response_id":"resp_01","created_at":"2026-07-30T14:25:30.900Z","item_id":"msg_01","content_index":0,"delta":"The latest Pydantic AI release is "}

id: 6
event: response.output_text.delta
data: {"type":"response.output_text.delta","sequence_number":6,"response_id":"resp_01","created_at":"2026-07-30T14:25:31Z","item_id":"msg_01","content_index":0,"delta":"GHS 42.50."}

id: 7
event: response.completed
data: {"type":"response.completed","sequence_number":7,"response_id":"resp_01","created_at":"2026-07-30T14:25:31.100Z","response":{"id":"resp_01","object":"response","agent_id":"agent_general_assistant","conversation_id":"conv_01J...","status":"completed","created_at":"2026-07-30T14:25:30Z","started_at":"2026-07-30T14:25:30.100Z","completed_at":"2026-07-30T14:25:31.100Z","cancellation_reason":null,"output":[{"id":"msg_01","type":"message","role":"assistant","content":[{"type":"output_text","text":"The latest Pydantic AI release is 2.14.1."}]}],"required_action":null,"error":null,"usage":{"input_tokens":84,"output_tokens":12,"total_tokens":96},"metadata":{}}}

```

## 15. Conformance checklist

An implementation conforms to this specification when:

- [ ] all production endpoints use HTTPS;
- [ ] authentication and resource ownership are enforced;
- [ ] create operations support idempotency and timeout recovery without duplicate execution;
- [ ] JSON and SSE media types are correct;
- [ ] SSE records contain complete JSON payloads;
- [ ] SSE IDs equal gap-free sequence numbers and retained events replay deterministically;
- [ ] at most one active response exists per conversation;
- [ ] every execution reaches an explicit terminal or requires-action state;
- [ ] approval, rejection, expiry, and repeated decisions have defined outcomes;
- [ ] completed responses contain the finished result, not an acknowledgement;
- [ ] responses remain retrievable after stream disconnection;
- [ ] cancellation is explicit and idempotent;
- [ ] pre-stream errors use Problem Details;
- [ ] mid-stream fatal errors use `response.failed`;
- [ ] unknown fields and events are forward-compatible;
- [ ] private reasoning, secrets, and unsafe tool payloads are not exposed;
- [ ] long streams use heartbeats and bounded buffering;
- [ ] data retention, rate limits, and deprecation policy are documented.
