# Agent Service SLOs

**Objective version:** 1.0  
**Effective:** 2026-08-03  
**Scope:** interactive text responses in the current single-worker POC

These are deployment objectives, not universal architecture constants. Production values must be
re-baselined from representative load tests and the versioned evaluation suite.

## Initial objectives

| Signal | Default objective | Measurement |
|---|---:|---|
| Queue delay | p95 ≤ 250 ms | Response `created_at` to `started_at` |
| Time to first text | p95 ≤ 3,000 ms | Execution start to first public `response.output_text.delta` |
| Completion duration | p95 ≤ 30,000 ms | Execution start to terminal response state |
| Model request duration | Observe by model, provider, agent and outcome | Time around each Pydantic AI model request |
| Token usage | Observe input/output totals for every completed provider response | Provider-reported usage, aggregated per public response |
| Estimated model cost | Record when pricing is known | Per-request estimate from the pinned `genai-prices` snapshot |

The first three objectives are configured through `AGENT_SLO_QUEUE_DELAY_MS`,
`AGENT_SLO_TIME_TO_FIRST_TEXT_MS`, and `AGENT_SLO_COMPLETION_MS`. Each terminal response persists
the observed latency and objective result in `usage.latency` and `usage.slo`.

## Telemetry

The agent emits these OpenTelemetry/Logfire metrics without prompt or tool content:

- `agent.response.queue_delay`
- `agent.response.time_to_first_text`
- `agent.response.completion_duration`
- `agent.model.request.duration`
- `agent.model.tokens`
- `agent.model.cost.estimated`
- `agent.turn.duration`

Model, provider and agent labels are bounded configuration identifiers. Response, subject and
conversation IDs are deliberately excluded from metric labels; those correlations belong in
traces and durable response records.

## Release gate

`uv run voice-ai evals` runs the deterministic contract gate and makes no model or network calls.
`uv run --env-file .env voice-ai evals --live` deliberately exercises the configured model and
tools against the same versioned cases. A model, prompt, tool, harness or gateway change is not
eligible for release when the live suite misses its declared assertion threshold.

## Remaining production objectives

Availability, terminal error rate, cancellation acknowledgement, event replay success, deletion
completion and voice time-to-first-audio still require production traffic windows and alert rules.
They remain explicit conformance gaps rather than being inferred from the three interactive
latency objectives above.
