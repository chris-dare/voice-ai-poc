# Agent Service Specification

**Status:** Draft  
**Specification version:** `0.1`  
**Last updated:** 2026-08-02  
**Audience:** architects, implementers, security engineers, operators, and test engineers

This document specifies the behavior and internal service boundaries of a general-purpose AI
agent service. It is intentionally concerned with capabilities, interfaces, invariants, and
observable guarantees. A conforming implementation may use any technology that satisfies those
requirements.

The words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are normative. A normative
requirement is assigned a stable identifier and must be verifiable through an API result, emitted
event, persisted record, audit record, telemetry attribute, or documented deployment artifact.
Statements without a requirement identifier are explanatory.

The [Public AI Agent API Specification](./AGENT_API_SPEC.md) is authoritative for public HTTP,
JSON, and SSE wire shapes. This specification is authoritative for the execution, state, policy,
security, and recovery semantics behind that API. Implementations must resolve any inconsistency
between the documents rather than silently choosing one behavior.

## 1. Purpose and scope

The agent service accepts user work, maintains conversation state, invokes models and tools,
coordinates bounded agentic work, streams safe progress, and persists the result. It is a separate
deployment boundary from user interfaces, voice processing, and domain systems.

```text
External clients
  text UI ─┐
  voice I/O ├── public Agent API ── Agent Service ── models / tools / data systems
  API users ┘
```

The service owns:

- authenticated conversation and response resources;
- execution lifecycle, cancellation, replay, and idempotency;
- context assembly and history compaction;
- model selection and invocation;
- capability discovery, tool execution, and specialist delegation;
- server-side policy, approvals, budgets, and audit;
- usage, quality, reliability, and security telemetry.

The service does not own:

- browser layout, microphone capture, speech recognition, or speech synthesis;
- the business logic of independently operated domain systems;
- arbitrary client-provided system prompts or client-selected credentials;
- a fleet-wide agent marketplace unless the Managed Platform profile is enabled.

**AS-SCOPE-001 — Core:** The service MUST expose agent work through the resources and lifecycle
defined by the public API specification. Verification: create, retrieve, stream, and cancel a test
response.

**AS-SCOPE-002 — Core:** Modality-specific clients MUST be replaceable without moving model,
history, policy, or tool authority outside the agent service. The service MUST NOT require any
modality-specific field, such as audio, transcript confidence, viewport, or rendering hints, to
accept, execute, or complete a response. Verification: invoke the same agent and conversation
through two authorized clients while omitting modality-specific fields and observe equivalent
service semantics.

## 2. Architectural model

```text
┌──────────────────────────────── AGENT SERVICE ────────────────────────────────┐
│                                                                               │
│  ┌──────────── Agent API boundary ────────────┐                               │
│  │ auth · ownership · scopes · rate limits    │                               │
│  │ idempotency · request IDs · cancel · SSE   │                               │
│  └───────────────────┬────────────────────────┘                               │
│                      │                                                        │
│  ┌───────────────────▼────────────────────────┐                               │
│  │ Durable response coordinator               │                               │
│  │ conversations · responses · events         │                               │
│  │ history · required actions · usage         │                               │
│  └───────────────────┬────────────────────────┘                               │
│                      │                                                        │
│  ┌───────────────────▼─────────────────────────────────────────────────────┐  │
│  │ Execution plane                                                       │  │
│  │ root assistant ─ progressive capabilities ─ bounded specialists       │  │
│  │        │                 │                         │                    │  │
│  │        └──────── policy enforcement + budgets ─────┘                    │  │
│  └───────────────┬──────────────────────────────┬──────────────────────────┘  │
│                  │                              │                             │
│  ┌───────────────▼──────────────┐  ┌───────────▼─────────────────────────┐   │
│  │ Model plane                  │  │ Tool and data plane                │   │
│  │ routing · fallback · gateway │  │ sandbox · web · remote tools       │   │
│  └──────────────────────────────┘  └─────────────────────────────────────┘   │
│                                                                               │
│  Cross-cutting: security · audit · telemetry · evals · data lifecycle         │
└───────────────────────────────────────────────────────────────────────────────┘

Optional extensions:
  durable workflow engine (long-running/restart-safe work)
  managed control plane and agent-to-agent interoperability (platform scale)
```

### 2.1 Component responsibilities

The API boundary authenticates and authorizes callers, validates public requests, applies quotas,
and maps internal state to the public contract. It does not decide which tool should answer a
particular wording.

The response coordinator is the durable authority for conversation ordering, response state,
event sequencing, idempotency, and required actions. Streaming transports observe this state; they
do not own it.

The execution plane decides whether to answer directly, use capabilities, delegate bounded work,
or enter a deterministic workflow. The model may propose work, but server code enforces policy and
budgets.

The model plane adapts configured model hosts behind a common internal contract. The tool and data
plane adapts local and remote capabilities behind validated schemas and explicit trust boundaries.

**AS-ARCH-001 — Core:** Durable state MUST remain authoritative when an API or streaming
connection disconnects. Verification: disconnect an active stream, reconnect by response ID and
cursor, and observe the same execution and retained events.

**AS-ARCH-002 — Core:** Model- and tool-specific payloads MUST be normalized at their adapter
boundaries before they affect resource state. Verification: contract tests run the same mocked-turn
fixture suite through every configured adapter and a reference test adapter and produce equivalent
normalized resource state.

## 3. Terminology and state

- **Principal:** the authenticated human, service, or agent identity making a request.
- **Tenant:** the primary data-isolation and policy boundary.
- **Agent definition:** a versioned server-controlled configuration of instructions, capabilities,
  model policy, and safety policy.
- **Conversation:** an ordered, durable container for multi-turn context.
- **Response:** one execution against a conversation.
- **Event:** an immutable, ordered observation of response progress.
- **Capability:** a coherent set of instructions and tools that may be made available to an agent.
- **Tool call:** a model-proposed invocation of a capability boundary.
- **Tool run:** the server-controlled execution record for a tool call.
- **Specialist:** a bounded agent invoked by the root assistant for a self-contained subtask.
- **Required action:** a paused decision that an authorized principal must approve or reject.
- **Effect:** a change made outside the agent service that cannot be undone by cancelling text
  generation.
- **Workflow:** a deterministic, checkpointed control flow that may survive process restarts.

### 3.1 Response state machine

```text
queued ──start──► in_progress ────────────────► completed
  │                  │  │  └─────────────────► failed
  │                  │  └────────────────────► cancelled
  │                  │
  │                  └──► requires_action ──approve/reject──► in_progress
  │                                  │
  │                                  └──expire/cancel───────► cancelled
  ├─────────────────────────────────────────────────────────► failed
  └─────────────────────────────────────────────────────────► cancelled
```

`completed`, `failed`, and `cancelled` are terminal. `requires_action` is paused but non-terminal.
A rejected required action resumes the response without performing the proposed effect; only expiry
or an explicit cancel terminates a paused response. Tool-run rejection is distinct: the tool run
becomes `cancelled` while the response returns to `in_progress`. Cancellation requests are best
effort: an effect already committed by a tool is not rolled back merely because the response later
becomes `cancelled`.

**AS-STATE-001 — Core:** A response MUST follow only the transitions above and MUST emit exactly
one terminal event unless it is paused in `requires_action`. Verification: state-transition tests
cover every allowed and rejected edge.

**AS-STATE-002 — Core:** A conversation MUST NOT have more than one response in a non-terminal state
(`queued`, `in_progress`, or `requires_action`) at any time. Verification: concurrent creation
attempts yield exactly one active response and one `conversation_busy` conflict.

**AS-STATE-003 — Core:** Terminal response state and retained terminal output MUST be immutable.
Verification: delayed provider or tool callbacks cannot modify a terminal response.

### 3.2 Tool-run state

```text
proposed ──allow──────────────────────────────► running ──► succeeded
   │                                             ├───────► failed
   ├──require approval──► awaiting_approval      ├───────► cancelled
   │                              ├──approve─────┤
   │                              ├──reject─────► cancelled
   │                              └──expire─────► cancelled
   └──policy deny───────────────────────────────► cancelled
                                                 └──ambiguous──► outcome_unknown
                                                                      ├──► succeeded
                                                                      └──► failed
```

**AS-STATE-004 — Effectful Tools:** Each effectful tool call MUST have a durable tool-run record
containing the response ID, tool identity and version, argument digest, policy decision, approval
decision where applicable, attempt count, timestamps, outcome, and safe effect summary.
Verification: inspect the audit record after success, denial, timeout, and retry.

**AS-STATE-005 — Effectful Tools:** An ambiguous effect outcome MUST NOT be represented as a safe
retryable failure. It MUST enter a reconciliation path that determines whether the effect committed
before any retry or compensation. Verification: interrupt the downstream connection after request
acceptance but before acknowledgement and observe no automatic duplicate effect.

## 4. Conformance profiles

Profiles are additive capability sets, not maturity levels. Every deployment implements Core and
adds other profiles when their trigger applies. No additive profile relaxes Core or another enabled
profile; a deployment's effective obligations are the union of all enabled profiles' requirements.

| Profile | Trigger | Additional concerns |
|---|---|---|
| **Core** | Every deployment | authenticated tenancy, durable resources, streaming, tools without external side effects, bounded execution |
| **Effectful Tools** | Any enabled tool can produce an effect outside the agent service's own durable state | approval, effect idempotency, argument binding, compensation and audit |
| **Durable Workflows** | Active execution must continue across process restart or redeployment instead of terminating as a retryable failure | checkpoints, leases, heartbeats, recovery and deterministic replay |
| **Managed Platform** | Multiple independently administered tenants or agent definitions are offered as a platform | control plane, delegated administration, residency, versioning and interoperability |

**AS-PROFILE-001 — Core:** A deployment MUST publish a machine-readable capability manifest to
operators through a deployment artifact or authenticated operational endpoint. The manifest MUST
contain `spec_version`, `profiles`, `optional_features`, and `limits`. Profile tokens MUST be exactly
`core`, `effectful_tools`, `durable_workflows`, or `managed_platform`; `core` MUST be present.
`limits` MUST include every limit enumerated by AS-EXEC-002. Verification: validate the effective
manifest against these field and value rules and the following minimum shape.

```json
{
  "spec_version": "0.1",
  "profiles": ["core"],
  "optional_features": ["remote_tools", "specialist_delegation"],
  "limits": {
    "max_execution_seconds": 120,
    "max_required_action_pause_seconds": 3600,
    "max_model_requests": 12,
    "max_total_tokens": 100000,
    "max_tool_calls": 20,
    "max_output_bytes": 1048576,
    "max_delegation_depth": 1,
    "max_specialist_fanout": 3,
    "max_concurrent_work": 4
  }
}
```

**AS-PROFILE-002 — Core:** The manifest MUST be generated from effective configuration rather than
maintained as unrelated documentation. Verification: changing an enabled profile changes the
manifest and activates its conformance suite.

## 5. Identity, tenancy, and authorization

The tenant is the isolation boundary. Projects, organizations, and users may exist inside that
boundary, but must not weaken it.

**AS-ID-001 — Core:** Tenant and principal identity MUST derive from validated credentials or a
trusted identity lookup, never from an untrusted body, path, query, or metadata field.
Verification: a mismatched client-supplied identity is rejected and audited.

**AS-ID-002 — Core:** Every durable row, object, event partition, cache key, idempotency record,
task, lock, and telemetry correlation MUST be tenant-scoped. Verification: automated isolation
tests attempt cross-tenant access through every storage and cache path.

**AS-ID-003 — Core:** Authorization MUST be evaluated on every create, read, list, stream, cancel,
delete, tool, and required-action operation. Verification: scope-removal tests deny each operation
independently.

**AS-ID-004 — Core:** Downstream credentials MUST be resolved server-side for the authenticated
tenant and capability; credentials MUST NOT be accepted in prompts or returned to the model.
Verification: prompt and tool transcripts contain no usable credential material.

**AS-ID-005 — Managed Platform:** Delegated administrators MUST be restricted to explicit tenant,
agent, environment, and action scopes, with every administrative mutation audited. Verification:
cross-scope administration attempts fail without changing state.

## 6. Durable resources, events, and idempotency

Durable response resources are required by Core. This is distinct from Durable Workflows: Core
persists the work record and result; the additional profile also guarantees restart-safe execution.

**AS-DATA-001 — Core:** Conversation, response, required-action, idempotency, and retained-event
records MUST survive API process restart. Verification: restart the API between creation and
retrieval.

**AS-DATA-002 — Core:** Events MUST be immutable, gap-free, monotonically sequenced within a
response, and uniquely identified by `(response_id, sequence_number)`. Verification: replay all
events and compare their sequence and digest with the original stream.

**AS-DATA-003 — Core:** Event delivery is at least once. Clients MUST be able to deduplicate by
event ID, and a resumed stream MUST replay retained events after the supplied cursor before
following live work. Verification: reconnect at every cursor in a fixture response.

**AS-DATA-004 — Core:** An expired or evicted cursor MUST return the distinct public
`event_cursor_expired` conflict rather than silently replaying from zero. Verification: resume from
an intentionally evicted cursor.

**AS-DATA-005 — Core:** Idempotency scope and fingerprinting MUST follow the public API
specification. The same key and fingerprint returns the original operation; the same key with a
different fingerprint returns `idempotency_conflict`; concurrent duplicates MUST NOT duplicate
model or tool work. Verification: sequential and concurrent replay tests.

**AS-DATA-006 — Core:** Persisted usage MUST identify input tokens, output tokens, model requests,
tool calls, wall-clock duration, and the model route used. Cost SHOULD be persisted where pricing
is known. Verification: compare stored usage with adapter and tool telemetry for a fixture run.

**AS-DATA-007 — Core:** The deployment MUST publish retention periods for conversations, response
events, idempotency records, audit records, and telemetry. Verification: configuration and expiry
tests match the published periods.

**AS-DATA-008 — Core:** The persisted event envelope MUST carry an event-schema version, and
retained events MUST remain replayable by every deployment version supported during the published
event-retention period. Verification: after an upgrade, replay a fixture stream written by the
previous released version.

## 7. Execution and orchestration

The service chooses the least complex execution mode that can reliably satisfy the request:

```text
simple answer             -> root model only
tool-assisted answer      -> root model + bounded tool calls
complex open-ended work   -> plan and/or bounded specialist delegation
high-risk/stateful work   -> policy-gated deterministic workflow
```

This is semantic routing, not a phrase-to-tool lookup table. Tools are selected from schemas,
descriptions, policy, and context.

**AS-EXEC-001 — Core:** The root assistant MUST own the final answer and MUST NOT complete while a
promised tool call, specialist task, or required action remains outstanding. Verification: fixture
models that emit an acknowledgement before tool completion cannot produce `completed`.

**AS-EXEC-002 — Core:** Execution limits MUST include wall time, model requests, total tokens,
tool-call count, output size, delegation depth, specialist fan-out, and concurrent work.
Verification: exceed each limit independently and observe a safe, machine-readable exhaustion
reason.

**AS-EXEC-003 — Core:** Limits MUST be enforced by server code. Child runs and retries MUST debit
the parent response budget. Verification: delegated and retried runs cannot exceed the aggregate
budget.

**AS-EXEC-004 — Core:** A response MUST record the effective agent-definition version, capability
set, model route, limit policy, and policy version needed to reproduce or explain the run.
Verification: retrieve the execution audit projection for a completed response.

**AS-EXEC-005 — Core:** Cancellation MUST propagate to active model calls, cancellable tool runs,
and specialist runs. Where the Effectful Tools profile is enabled, the response MUST additionally
record committed effects that could not be cancelled. Verification: cancel during each stage and,
where applicable, inspect terminal state and effect summary.

**AS-EXEC-006 — Core:** Time spent in `requires_action` MUST be excluded from the execution
wall-time budget and MUST instead be bounded by a server-assigned required-action expiry. Expiry
MUST be computed and enforced from server time, MUST NOT be extendable by the client, and MUST NOT
exceed the configured maximum pause duration. Verification: a response paused past `expires_at`
transitions to `cancelled` with reason `action_expired` without consuming execution budget; a
client-supplied expiry is ignored and audited.

## 8. Context, history, and memory

The service must distinguish four data classes:

1. server-controlled instructions and policy;
2. trusted tenant or agent configuration;
3. conversation history and user input;
4. untrusted retrieved or tool-generated content.

**AS-CTX-001 — Core:** Clients and tool outputs MUST NOT be able to introduce or replace
server-controlled instructions through role labels, markup, metadata, or retrieved content.
Verification: prompt-injection fixtures cannot change policy or enable a denied tool.

**AS-CTX-002 — Core:** Context assembly MUST preserve causal ordering of user input, model output,
tool calls, tool results, and required-action decisions. Verification: round-trip stored history
through the execution adapter and compare typed message order.

**AS-CTX-003 — Core:** Compaction MUST preserve current instructions, unresolved commitments,
required actions, material tool results, safety constraints, and source provenance. Verification:
long-history tests retain each class after compaction.

**AS-CTX-004 — Core:** Retrieved and tool-produced content MUST carry provenance and an untrusted
classification until transformed into a validated result. Verification: provenance is visible in
the internal audit projection and security trace.

**AS-CTX-005 — Core:** Durable conversation history MUST remain separate from optional semantic or
user memory. Semantic memory MUST be tenant-scoped, permission-aware, provenance-bearing,
deletable, and disabled unless explicitly configured. Verification: disabling memory prevents both
retrieval and writes.

## 9. Progressive capabilities

A small always-visible tool set reduces prompt cost and tool confusion. Additional capabilities
may be discovered and loaded when needed. Loading changes availability, not authorization.

**AS-CAP-001 — Core:** Every capability MUST declare a stable ID and version, description, tools,
required scopes, side-effect class, trust class, and applicable limits. Verification: validate the
capability catalog before deployment.

**AS-CAP-002 — Core:** Capability discovery MUST filter by tenant policy and principal scope before
descriptions reach the model. Verification: unauthorized capability names and descriptions are
absent from the model request and catalog result.

**AS-CAP-003 — Core:** The effective tools visible to each model request and the capability changes
during a response MUST be recorded for audit and eval replay. Verification: reconstruct the tool
set for every model request in a fixture response.

**AS-CAP-004 — Core:** Tool names MUST be unique within a model request. Collisions across local or
remote toolsets MUST be resolved deterministically or reject startup. The chosen collision strategy
MUST be declared in the capability manifest. Verification: register two identically named tools
and observe the outcome declared in the manifest.

## 10. Tools and external systems

All tool descriptions, arguments, and results cross a trust boundary. Remote tool protocols are
integration mechanisms, not authorization systems.

**AS-TOOL-001 — Core:** Every tool MUST expose a versioned input schema, bounded output schema or
content contract, timeout, retry policy, side-effect classification, and safe progress label.
Verification: catalog validation rejects an incomplete tool.

**AS-TOOL-002 — Core:** The server MUST validate arguments immediately before invocation and
validate or safely bound results before inserting them into history. Verification: malformed model
arguments and malformed remote results cannot reach execution or history.

**AS-TOOL-003 — Core:** Tool results, web pages, remote schemas, and remote tool descriptions MUST
be treated as untrusted input and MUST NOT override policy or instructions. Verification: indirect
prompt-injection tests remain contained.

**AS-TOOL-004 — Core:** Tool retries MUST be limited to classified transient failures. A retry MUST
respect the response deadline and budget and MUST NOT duplicate a non-idempotent effect.
Verification: fault-injection tests distinguish transient, permanent, and ambiguous outcomes.

**AS-TOOL-005 — Core:** Secrets and sensitive data MUST be redacted before tool output enters model
history, events, logs, or traces. Verification: seeded canary secrets are absent from all four
destinations.

**AS-TOOL-006 — Core:** Web access MUST apply scheme and host validation, redirect limits, response
size limits, timeouts, private-network blocking by default, and configurable egress policy.
Verification: SSRF and oversized-content fixtures are blocked.

**AS-TOOL-007 — Core:** Code execution MUST have explicit CPU, memory, wall-time, output, filesystem,
network, and process limits. It MUST inherit no ambient service credentials. Verification: sandbox
escape, secret-read, egress, fork, and resource-exhaustion tests.

**AS-TOOL-008 — Core, when remote tool servers are enabled:** Remote servers MUST be authenticated
and tenant-allowlisted; advertised schemas and returned data MUST be validated; connection failure
MUST not make the whole service unready unless the server is declared required. Verification:
unauthorized, malformed, and unavailable server tests.

## 11. Policy, approvals, and effects

The model proposes actions. The server decides whether an action is allowed, denied, or requires
approval. Prompt instructions may explain policy but are never the enforcement boundary.

**AS-POL-001 — Core:** Policy MUST be enforced at the tool-invocation boundary using authenticated
identity, tenant, agent version, tool version, validated arguments, data classification, and
runtime context. Verification: a model-proposed denied call is blocked and audited regardless of
prompt text.

**AS-POL-002 — Core:** The default for an unknown tool, unknown policy result, policy timeout, or
invalid arguments MUST be deny. Verification: each failure produces no tool invocation.

**AS-POL-003 — Effectful Tools:** An approval MUST bind to a single tool-run ID, exact tool version,
canonical argument digest, requester, tenant, expiry, and policy version. Verification: changing
any bound value invalidates the approval.

**AS-POL-004 — Effectful Tools:** Approvals MUST be single-use and idempotently decidable. Expiry
or decision timeout MUST deny execution. Verification: repeated approval returns the original
decision and cannot produce a second effect.

**AS-POL-005 — Effectful Tools:** The service MUST revalidate authorization and policy after
approval and immediately before execution. Verification: revoked access between approval and
execution prevents the effect.

**AS-POL-006 — Effectful Tools:** Every effectful tool MUST declare its idempotency strategy and,
where feasible, compensation operation. The service MUST surface committed effects and MUST NOT
claim that cancellation rolled them back. Verification: cancel after commit and inspect the safe
effect summary.

**AS-POL-007 — Effectful Tools:** A deployment MUST configure whether requester and approver may be
the same principal for each risk class. Verification: separation-of-duty tests follow effective
policy.

## 12. Specialist delegation

Delegation is appropriate for a self-contained subtask whose result returns to the root assistant.
Deterministic application control flow is preferable when the next step is fixed. A workflow or
graph is preferable when correctness depends on explicit state transitions.

**AS-DELEG-001 — Core, when delegation is enabled:** A specialist invocation MUST include a task,
expected output contract, deadline, budget, allowed capabilities, and correlation to its parent
response. Verification: the delegation record contains every field.

**AS-DELEG-002 — Core, when delegation is enabled:** A specialist's permissions and capabilities
MUST be a subset of its parent's effective set unless a separately authorized server policy grants
an explicit exception. Verification: attempted privilege escalation is denied and audited.

**AS-DELEG-003 — Core, when delegation is enabled:** Delegation MUST enforce depth, fan-out,
concurrency, token, request, tool, cost, and wall-time bounds. Cycle detection MUST prevent recursive
agent loops. Verification: cycle and exhaustion fixtures terminate safely.

**AS-DELEG-004 — Core, when delegation is enabled:** Specialist history SHOULD be isolated and
limited to the task-relevant context. The root assistant MUST synthesize the user-facing answer and
must not expose private specialist reasoning. Verification: specialist-only context is absent from
public output and unrelated specialists.

## 13. Model routing and hosting

The orchestration runtime depends on an internal model contract rather than provider-specific
types. Configuration may route to a direct provider, cloud platform, AI gateway, or local host.

**AS-MODEL-001 — Core:** A model adapter MUST support typed messages, tool schemas and calls,
streaming text, safe provider errors, cancellation where available, usage, and model identity.
Verification: adapter conformance tests exercise the same fixture suite.

**AS-MODEL-002 — Core:** Each response MUST record the selected route, canonical model ID,
effective model settings, attempt number, and fallback reason without recording credentials.
Verification: inspect telemetry and the execution audit projection.

**AS-MODEL-003 — Core:** Fallback MUST occur only for classified transient availability or capacity
failures. Authentication, authorization, invalid request, content-policy, or ordinary low-quality
output MUST NOT trigger fallback. Verification: inject every error class and inspect attempts.

**AS-MODEL-004 — Core:** A fallback route MUST satisfy the tenant's tool, data retention, residency,
security, and modality constraints. Otherwise the response MUST fail safely. Verification: a
disallowed route receives no tenant content.

**AS-MODEL-005 — Core:** Production deployments MUST use explicit model versions or a controlled
alias whose changes are canaried and evaluation-gated. Verification: effective configuration maps
to a release record and eval result.

**AS-MODEL-006 — Core:** Readiness checks MUST validate configuration and zero-cost local
dependencies without making a billable generation request. Verification: repeated probes incur no
model generation usage.

## 14. Reliability and durable workflows

Core guarantees that response state and completed results survive a restart. If an active Core
execution cannot resume after a restart, it must become a safe retryable failure rather than remain
indefinitely active. Durable Workflows adds continuation of active execution.

**AS-REL-001 — Core:** Every accepted response MUST reach a terminal or `requires_action` state
within a configured maximum duration. A recovery process MUST reconcile abandoned active records.
Verification: terminate a worker and observe bounded reconciliation.

**AS-REL-002 — Core:** API and streaming instances SHOULD be stateless apart from bounded caches.
Authoritative task, event, idempotency, lock, and ownership state MUST NOT exist only in process
memory when more than one instance is deployed. Verification: route successive requests to
different instances.

**AS-REL-003 — Core:** Backpressure MUST be bounded at request, response, tenant, model-route, tool,
and stream buffers. Overload MUST reject or queue predictably without unbounded memory growth.
Verification: sustained overload test observes configured bounds.

**AS-WF-001 — Durable Workflows:** Active work MUST use durable checkpoints plus leased ownership,
heartbeats, lease expiry, and an orphan reaper. Verification: kill a worker at every checkpoint and
observe exactly one recovered logical execution.

**AS-WF-002 — Durable Workflows:** Workflow state transitions MUST be deterministic under replay.
Model and tool calls MUST occur as recorded external activities rather than inside replayed control
logic. Verification: replay a recorded workflow without issuing duplicate external calls.

**AS-WF-003 — Durable Workflows:** Activity delivery MUST be treated as at least once. Effectful
activities MUST use downstream idempotency keys, deduplication, or an explicit compensation plan.
Verification: duplicate activity delivery produces at most one committed logical effect.

**AS-WF-004 — Durable Workflows:** Workflow upgrades MUST define compatibility for in-flight
executions through versioning, patch markers, or draining. Verification: deploy an upgrade while an
old workflow is paused and resume it successfully or fail it by documented policy.

## 15. Observability, audit, and evaluation

Diagnostic telemetry and security audit serve different purposes. Diagnostic content may be
sampled or redacted; security-relevant decisions require durable, access-controlled audit records.

**AS-OBS-001 — Core:** Distributed traces MUST correlate request, tenant-safe principal, agent,
conversation, response, model attempt, tool run, specialist run, and workflow IDs. Verification:
trace a fixture response end to end.

**AS-OBS-002 — Core:** Metrics MUST include request rate, active responses, queue delay,
time-to-first-event, time-to-first-text, total latency, model latency and errors, tool latency and
errors, token usage, cost where known, cancellations, policy decisions, and terminal outcomes.
Verification: each fixture changes the expected metric.

**AS-OBS-003 — Core:** Prompts, model content, tool arguments, and tool results MUST be excluded from
telemetry by default or processed through explicit redaction and access policy. Secrets MUST never
be recorded. Verification: canary-data scan across telemetry storage.

**AS-OBS-004 — Core:** Audit records MUST be append-only for authentication failures, authorization
decisions, policy results, approvals, effectful tool outcomes, agent-definition changes, and data
deletion. Verification: each action yields an immutable actor/time/target/outcome record.

**AS-EVAL-001 — Core:** Every agent release or model-route change MUST be gated by a versioned eval
suite whose cases cover answer quality, instruction following, tool selection, tool completion,
multi-turn context, cancellation, security boundaries, latency, and cost, and whose pass thresholds
are declared in deployment configuration. A release MUST NOT proceed when a declared threshold is
unmet. Verification: release metadata references a passing eval run and its configured thresholds.

**AS-EVAL-002 — Core:** Evals MUST include deterministic contract tests, scenario datasets,
adversarial prompt-injection tests, and failure injection. Model-graded results MUST be calibrated
against human-reviewed examples. Verification: inspect the eval suite and calibration report.

**AS-EVAL-003 — Core:** Production feedback and failures SHOULD be converted into privacy-reviewed
regression cases. Evaluation datasets MUST honor the source data's tenant, consent, retention, and
deletion rules. Verification: provenance and deletion propagation tests.

## 16. Data lifecycle and security

**AS-SEC-001 — Core:** Data MUST be encrypted in transit and at rest. Service-to-service identity
and least-privilege authorization MUST apply to model, tool, database, event, and telemetry
connections. Verification: configuration and connection tests.

**AS-SEC-002 — Core:** The service MUST maintain a data-flow inventory covering every model route,
tool, store, cache, log, trace, backup, and evaluation dataset that may receive tenant content.
Verification: inventory entries match runtime egress observations.

**AS-SEC-003 — Core:** Conversation deletion MUST propagate to primary data, retained events,
derived memory, blobs, caches, and eligible telemetry/eval copies within a published SLA. Legal
holds or required audit retention MUST be disclosed and access-controlled. Verification: seeded
deletion markers disappear from each eligible store.

**AS-SEC-004 — Core:** Supply-chain controls MUST pin production dependencies, verify build
artifacts, scan known vulnerabilities, and record a software bill of materials. Verification:
release artifacts include provenance, signatures or checksums, and an SBOM.

**AS-SEC-005 — Core:** Agent, tool, policy, model-route, and prompt changes MUST be versioned,
reviewable, auditable, and rollback-capable. Verification: deploy and roll back a canary version
without changing historical response attribution.

**AS-SEC-006 — Managed Platform:** Tenant residency, retention, model-route, tool-egress, and key-
management policy MUST be enforceable per tenant and must constrain fallback and delegation.
Verification: policy matrix tests show no cross-class route.

## 17. Managed platform and interoperability

The Managed Platform profile is justified when independently administered agent definitions and
tenants need lifecycle management. It should not be introduced merely to run one assistant.

**AS-PLAT-001 — Managed Platform:** Agent definitions MUST have immutable versions, lifecycle state,
owner, policy bindings, capability bindings, model policy, release metadata, and rollback target.
Verification: retrieve the control-plane record and invoke a pinned version.

**AS-PLAT-002 — Managed Platform:** Breaking changes to an agent, tool, policy, or interoperability
contract MUST use a published deprecation window and migration path. Verification: an older
supported client continues to function during the window.

**AS-PLAT-003 — Managed Platform:** Per-tenant concurrency, token, cost, storage, and external-tool
quotas MUST be enforceable with safe isolation of usage details. Verification: one tenant's quota
exhaustion does not affect another tenant outside shared capacity policy.

Remote tool protocols are the preferred boundary for tools, resources, and contextual capabilities.
An agent-to-agent protocol is appropriate only when the remote party is an independently operated
agent that owns its own planning, lifecycle, and result—not merely a tool disguised as an agent.

**AS-A2A-001 — Managed Platform, when interoperability with independently operated agents is
enabled:** The service MUST publish a versioned capability description, authenticate both parties,
authorize each delegated task, bind budgets and deadlines, propagate trace context, and expose
cancellable task state. Verification: protocol conformance and cross-tenant denial tests.

**AS-A2A-002 — Managed Platform, when interoperability with independently operated agents is
enabled:** Remote agents MUST be treated as untrusted external systems. Their results MUST pass the
same validation, provenance, policy, and content controls as tool results. Verification: malicious
remote-agent fixtures remain contained.

## 18. Operational objectives

Exact service-level objectives depend on deployment and workload, so this specification defines
what must be measured rather than imposing universal numbers.

**AS-SLO-001 — Core:** A deployment MUST publish objectives and alert thresholds for availability,
accepted-response durability, queue delay, time-to-first-event, time-to-first-text, completion
latency by workload class, cancellation acknowledgement, event replay success, terminal error rate,
and deletion completion. Verification: dashboards and alerts use the same metric definitions.

**AS-SLO-002 — Core:** Latency MUST be decomposable into queue, context assembly, first model token,
model generation, tool, specialist, persistence, and stream delivery time. Verification: a sampled
trace exposes each applicable stage.

**AS-SLO-003 — Core:** Capacity planning MUST account for concurrent responses, model and tool rate
limits, database connections, event retention, stream connections, sandbox resources, and worst-
case fan-out. Verification: load-test results support configured production limits.

## 19. Conformance

An implementation claims conformance for the exact profiles in its capability manifest. A claim
must name the specification version, deployment version, enabled optional features, and conformance
test result.

Minimum conformance evidence:

- a requirement matrix mapping every applicable `AS-*` identifier to an automated test, inspection,
  or documented operational control;
- black-box API and state-transition tests;
- tenant-isolation and authorization tests;
- replay, idempotency, disconnect, cancellation, and restart tests;
- model and tool adapter contract tests;
- policy, approval, prompt-injection, SSRF, sandbox, and secret-leak tests;
- load, backpressure, and failure-injection results;
- eval and release-gate results;
- retention and deletion propagation evidence.

**AS-CONF-001 — Core:** A deployment MUST NOT claim a profile when any applicable MUST requirement
is unimplemented or lacks passing evidence. Verification: validate the published conformance report
against the manifest and requirement matrix.

**AS-CONF-002 — Core:** Exceptions MUST identify the requirement, owner, risk, compensating control,
expiry, and approval. An exception does not constitute full conformance. Verification: inspect the
exception register.

## Appendix A. Recommended implementation stack (non-normative)

**Recommendation date:** 2026-08-02. This appendix is excluded from conformance and should be
re-evaluated against current releases, security advisories, workload evals, operational experience,
and total cost before each major implementation decision.

### A.1 Practical stack for this project

| Concern | Recommended starting point | Selection note |
|---|---|---|
| Language and service API | Python 3.12+, FastAPI, Pydantic, Uvicorn | Strong typed-contract and async ecosystem; keep domain logic outside HTTP handlers. |
| Agent runtime | Pydantic AI 2.x; compare LangGraph or a provider SDK with a custom coordinator | Any runtime satisfying the A.3 model- and tool-adapter contracts is acceptable; evaluate alternatives because this is a large dependency surface. |
| Deep-agent harness | Pydantic AI Harness; compare LangChain Deep Agents or a custom capability layer | Useful for on-demand planning, files, subagents, output limiting, compaction, and sandbox integrations; adopt capabilities selectively. |
| Validation and configuration | Pydantic and Pydantic Settings | Validate every boundary and effective deployment configuration. |
| Persistence | PostgreSQL, SQLAlchemy async, asyncpg, Alembic | Keep conversations, responses, events, idempotency, approvals, and audit durable. |
| Short work dispatch | Transactional outbox plus a bounded worker queue | Avoid dual-write gaps. A database-backed queue is sufficient before high scale. |
| Durable workflows | Kitaru first; compare Temporal, Restate, DBOS, or Prefect | Introduce only for long-running or approval-heavy work. Prove crash recovery, replay, cancellation and idempotency in a focused spike before routing production work through it. |
| Model gateway | OpenRouter initially; reassess LiteLLM or AAIF agentgateway if self-hosting becomes necessary | Use the native Pydantic AI OpenRouter provider, keep model IDs configuration-driven, and avoid duplicating retry ownership across the service and gateway. Validate latency, cost attribution, credential isolation and tool conformance continuously. |
| Tool interoperability | MCP SDK or FastMCP-compatible client/server | Put independently deployed domain capabilities behind authenticated MCP boundaries. |
| Code execution | Pydantic Monty for pure computation; isolated container or microVM for OS/file/browser work | An in-process language sandbox is not a substitute for OS isolation when host capabilities are required. |
| Policy | Application policy layer first; Cedar or OPA when rules require independent lifecycle; OpenFGA for relationship authorization | Keep enforcement at the invocation boundary and policy versions in audit. |
| Coordination at scale | PostgreSQL advisory/row locks initially; Redis Streams, NATS JetStream, or Kafka when workload evidence justifies them | Never make an in-memory lock authoritative across replicas. |
| Telemetry | OpenTelemetry APIs with Logfire or another compatible backend | Keep the instrumentation contract portable and content capture controlled. |
| Evaluation | Pydantic Evals or promptfoo, plus pytest and security/load tooling | Gate agent, prompt, model, policy, and tool changes on the same scenario suite and keep cases portable. |
| Identity | Any standards-compliant OAuth 2.0/OIDC provider | Keep issuer/audience/scopes and tenant mapping in service configuration. |

[Pydantic AI documentation](https://pydantic.dev/docs/ai/guides/multi-agent-applications/), content
current as of the recommendation date. Package versions should be pinned in deployable artifacts
but should not appear in the normative architecture.

### A.2 Model-role candidates

Do not select one model for every role by reputation alone. Establish a representative eval set,
then select the smallest and least expensive model that passes each role's quality, tool-use,
latency, security, residency, and availability gates. Production should use pinned model IDs where
the host supports them.

| Role | Candidates as of the recommendation date | Guidance |
|---|---|---|
| Balanced root assistant | `claude-sonnet-5`, `gpt-5.6-terra`, `gemini-3.6-flash` | Start here for interactive tool use; compare first-token latency and multi-step completion on local evals. |
| Highest-capability deep work | `claude-fable-5`, `claude-opus-4-8`, `gpt-5.6-sol` | Reserve for difficult planning, synthesis, or escalation paths that justify cost and latency. |
| Fast specialist or high-volume path | `claude-haiku-4-5-20251001`, `gpt-5.6-luna`, `gemini-3.5-flash-lite` | Suitable candidates for classification, extraction, summaries, and bounded specialists after evals. |
| Local or controlled hosting | `gpt-oss-20b`, `gpt-oss-120b`, or another tool-capable open-weight model sized to the hardware | Validate tool calling, structured output, context limits, concurrency, and memory use on the actual host. |

Current official catalogs describe
[Claude model roles and pinned IDs](https://platform.claude.com/docs/en/about-claude/models/overview),
[OpenAI's Sol/Terra/Luna trade-offs](https://developers.openai.com/api/docs/models), and
[Gemini stable and preview lifecycle](https://ai.google.dev/gemini-api/docs/models). Treat preview,
experimental, and floating `latest` aliases as canary-only unless a deployment explicitly accepts
their change and deprecation risk.

### A.3 Substitution boundaries

A package or host can be replaced without redesigning the service when these internal contracts
remain stable:

- model adapter: typed messages, tools, streaming, usage, cancellation, safe errors;
- tool adapter: schema, policy metadata, identity, timeout, result validation, audit;
- state store: transactions, ownership checks, event append/replay, idempotency;
- work dispatcher: enqueue, lease, heartbeat, cancel, retry, dead-letter behavior;
- policy engine: allow/deny/require-approval plus reason and version;
- telemetry: spans, metrics, logs, audit correlation, redaction;
- eval runner: versioned cases, repeatable inputs, scores, thresholds, release decision.

## Appendix B. Current implementation snapshot (non-normative)

As of 2026-08-03, the current project implements the Core profile foundation:

- implemented: separate agent-service boundary, authenticated tenant-owned resources, durable
  conversations/responses/events/idempotency/history, JSON and resumable SSE, cancellation,
  configurable model routes, transient-only fallback, progressively loaded capabilities, sandboxed
  computation, web tools, optional MCP, bounded specialists, and distributed telemetry;
- deployment choice: Auth0 currently provides OAuth 2.0/OIDC identity;
- implemented foundation: per-response and per-model-attempt usage, estimated cost where pricing is
  known, interactive latency objectives, a versioned deterministic/live evaluation gate,
  PostgreSQL-backed bounded dispatch, leased worker ownership, heartbeats, retry exhaustion,
  shared rate limits, fleet-wide leased model/tool capacity, cross-replica durable event
  visibility, immutable response execution snapshots with an owner-scoped audit projection, and a
  machine-readable capability manifest generated from effective configuration;
- partial: representative eval coverage and calibration, comprehensive policy enforcement,
  deletion propagation evidence, and checkpoint recovery inside a model/tool turn;
- not claimed: Effectful Tools, Durable Workflows, or Managed Platform conformance. Required-action
  API scaffolding alone is not Effectful Tools conformance, and persisted response state alone is
  not Durable Workflows conformance.

This snapshot is informational. The requirement matrix and automated evidence determine actual
conformance.
