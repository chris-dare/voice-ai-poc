# Agent Service SLOs

**Objective version:** 1.0  
**Effective:** 2026-08-03  
**Scope:** interactive text responses using independently scalable API and worker replicas

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
| Billed model cost | Record when the provider reports it | OpenRouter per-response usage accounting, aggregated across root and specialist calls |
| Estimated model cost | Record independently when pricing is known | Cross-check from the pinned `genai-prices` snapshot |
| Accepted-response terminality | ≥ 99.9% within the maximum execution window | Accepted responses reaching completed, failed, cancelled, or requires-action |
| Worker recovery | p95 ≤ lease TTL + 5 s | Expired lease to claim by another healthy worker |
| Event visibility across replicas | p95 ≤ 1,000 ms | Durable event commit to SSE observation on another API replica |

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
- `agent.model.cost.reported`
- `agent.model.cost.estimated`
- `agent.turn.duration`
- `agent.worker.job`
- `agent.worker.queue_depth`
- `agent.worker.recovery.delay`
- `agent.execution.capacity`
- `agent.execution.capacity_wait`
- `agent.response.lifecycle`
- `agent.response.active`
- `agent.tool.duration`
- `voice.session.lifecycle`
- `voice.session.active`

Model, provider and agent labels are bounded configuration identifiers. Response, subject and
conversation IDs are deliberately excluded from metric labels; those correlations belong in
traces and durable response records. Standard Pydantic AI signals such as
`gen_ai.client.token.usage` and `operation.cost` are emitted alongside these application metrics.
The telemetry pipeline uses OpenTelemetry conventions; Logfire is the current backend and UI, not
the service's persistence system of record.

## Release gate

`uv run voice-ai evals` runs the deterministic text contract gate and makes no model or network
calls. `uv run voice-ai evals --modality voice` does the same for the voice interaction contract.
The text suite is versioned separately from the voice suite so changes to answer/tool behavior do
not silently recalibrate ASR, interruption, or audio-latency expectations.

`uv run --env-file .env voice-ai evals --live` deliberately exercises the configured model and
tools against the text cases. `AGENT_EVAL_JUDGE_MODEL` or `--judge-model provider:model` enables
case-specific LLM judges; otherwise the live gate uses only deterministic assertions. A model,
prompt, tool, harness or gateway change is not eligible for release when the applicable live suite
misses its declared assertion threshold.

Live voice evaluation requires consented recorded-audio fixtures and an explicit hardware profile.
Until that runner is configured, `--live --modality voice` fails closed instead of treating contract
fixtures as evidence of production ASR or TTS quality.

## Release and operational evidence

Before increasing worker concurrency or voice sessions, run the concurrency and restart-recovery
tests and a representative soak against the intended model provider. Alert on queue depth growth,
no active worker heartbeat, exhausted attempts, terminal error rate, database pool saturation, and
SLO burn. Voice time-to-first-audio is owned by the voice deployment and requires a separate load
baseline for each hardware profile.
