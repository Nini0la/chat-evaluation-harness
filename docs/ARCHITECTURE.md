# Architecture

## Goals and boundaries

The harness evaluates remote models through two separate workflows: interactive primary-plus-shadow conversation and reproducible batch evaluation over externally authored JSONL suites. It stores exact returned text and operational evidence. It deliberately has no clinical workflow, dataset-authoring logic, structured-output repair, training-data pipeline, or dependency on Edge-IMCI.

## Runtime architecture

```text
Invited tester browser
  │ HTTPS (access cookie; chat/session/feedback APIs)
  ▼
FastAPI + Jinja/static UI on Cloud Run
  ├── ModelProvider ── HTTPS/Bearer ──► remote model (Modal later)
  ├── SQLAlchemy/Alembic ─────────────► Cloud SQL PostgreSQL
  ├── Secret references ──────────────► Secret Manager
  └── stdout/stderr ──────────────────► Cloud Logging
```

The browser never calls the model endpoint. Endpoint credentials are read only by the backend. The application is one deployable service to keep the MVP understandable and inexpensive.

## Components

- `app/main.py`: HTTP routes, lightweight access exchange, admin review, deployment registration.
- `app/chat_service.py`: pending-call-finalize transaction flow, history reconstruction, metric calculation.
- `app/providers/`: provider protocol, deterministic mock, HTTP adapter, and factory.
- `eval_runner/`: suite validation, durable execution/resume, deterministic checks, rubric judge, scoring, and reports.
- `app/models.py`: SQLAlchemy entities and invariants.
- `app/export.py`: complete per-turn JSONL records and analysis-friendly CSV flattening.
- `app/templates`, `app/static`: responsive chat/review UI. Model text is inserted with `textContent`, not interpreted as HTML.
- `migrations/`: authoritative schema evolution.
- `infra/`, `Dockerfile`, `cloudbuild.yaml`: production baseline.

## Request lifecycle

1. `POST /api/sessions` reads the active primary deployment and creates a UUID session referencing it, with zero to three optional red/green/blue assignments.
2. `POST /api/sessions/{id}/messages` creates and commits a pending turn before any model call.
3. Completed prior turns are ordered by `turn_number`; each contributes its user then assistant message. The current user message is appended.
4. The primary provider and each selected shadow provider are called exactly once. No silent interactive generation retry occurs. Each shadow history contains the common user turns and only that shadow's prior completed responses.
5. On success, the exact provider `text`, IDs, token counts, optional metadata, timestamps, and derived metrics finalize the primary turn or shadow response.
6. On a provider or recoverable application error, the same row is marked failed with a classified type and bounded diagnostic message. The tester receives an error but the attempt remains queryable.

A prior failed turn is retained for analysis but omitted from subsequent model context because no corresponding assistant response exists.

## Session pinning and model changes

`Session.model_deployment_id` is assigned once and has no update API. Every turn redundantly records that deployment for straightforward analysis and integrity checks. A partial unique index permits only one active deployment. Activation deactivates the old row before enabling the new one. Therefore:

```text
session S1 created → deployment v1
activate v2
S1 next turn       → still v1
new session S2     → v2
```

Optional `session_shadows` assignments add red/green/blue comparisons without changing the pinned primary or the zero-shadow fallback.

## Provider abstraction

`ModelProvider.generate(messages, deployment)` returns a `GenerationResult`; `stream` exists as a fallback interface. Interactive and batch modes both depend on this contract. Provider errors carry stable categories and retryability. The generic HTTP/Modal and Azure OpenAI adapters translate provider-specific transport into the same result envelope.

The HTTP adapter is provider-neutral. `modal` currently selects the same contract and is merely a registry label; no Modal SDK or deployment assumptions are embedded. Configuration JSON is forwarded as non-secret inference settings. Responses are not trimmed, rewritten, repaired, or otherwise improved.

## Data model

### `model_deployments`

Provider, model identity/version, non-secret endpoint reference and JSON configuration, creation/activation/deactivation timestamps, and active flag. A partial unique index enforces at most one active row.

### `sessions`

UUID, nullable pseudonymous browser tester ID, pinned deployment FK, creation/end timestamps, and optional client JSON metadata. The browser stores the current session UUID and a random tester UUID in local storage so refresh can restore history.

### `turns`

UUID, session FK, unique per-session sequence number, exact user/assistant text, four event timestamps, token counts, TTFT/inference/total durations, throughput, deployment FK, status/error evidence, provider request ID/metadata, and creation time.

### `feedback`

UUID, turn FK, optional `-1`/`1` rating, optional category/comment, and creation time. Feedback is never mandatory and multiple feedback events can be retained rather than destructively overwritten.

### `session_shadows` and `shadow_responses`

Session-scoped red/green/blue deployment assignments and per-turn responses. Shadow output has the same status, error, timing, token, request-ID, and provider-metadata evidence as the primary output.

### `eval_candidates`

A review marker for a primary or RGB response. Its export is provenance-only and never mutates or appends to an evaluation suite.

## Batch evaluation lifecycle

1. Strict Pydantic validation loads a domain-neutral JSONL suite and rejects duplicate item IDs.
2. A canonical manifest snapshots the suite hash, selected deployment/checkpoint endpoints and non-secret configuration, common generation settings, judge settings, execution controls, and contract versions.
3. The manifest hash becomes the stable run ID. New runs use exclusive directory creation; resume requires an exact manifest match.
4. Bounded workers call every candidate model with the same prompt and effective generation configuration. Each final response or exhausted error is appended and fsynced to `responses.jsonl`.
5. Only after the generation phase is durable do bounded judge workers run deterministic checks and the configured model judge.
6. Judge text is retained verbatim and validated against the structured score/points/reason/confidence contract. Invalid output remains an auditable judge failure.
7. Reports preserve per-model and model-by-category profiles, deterministic failure rates, rubric scores, combined scores, and severity-sorted failures.

No category name or expected behavior is interpreted as clinical truth. Refusal expectations, literal markers, rubric points, and metric tags are supplied by the external suite.

PostgreSQL uses JSON/UUID-compatible SQLAlchemy types. SQLite is intentionally supported for local/tests, not recommended for durable multi-instance production.

## Timing semantics

- `request_started_at`: recorded before committing the pending turn.
- `inference_started_at`: a trustworthy provider-relative inference-start offset applied to a timestamp captured immediately before the provider call; otherwise null.
- `first_token_at` / `time_to_first_token_ms`: a provider-relative first-token offset applied to that same provider-call timestamp; otherwise null for non-streaming providers.
- `response_completed_at` / `total_latency_ms`: completion and backend-observed total request duration (or an exact provider timeline supplied through the adapter).
- `inference_latency_ms`: completion minus trustworthy inference start, or an explicit provider duration; otherwise null.
- `tokens_per_second`: output tokens divided by inference seconds only when both values are known and duration is positive.

Null means unknown. No cold/warm label is inferred. `provider_request_id` and non-secret `provider_metadata` support later outlier investigation. Raw event fields permit median, p90, p95, maximum, and model-version distributions in downstream SQL/notebooks.

## Access and trust boundaries

Tester and admin codes are independent. Constant-time comparison protects exchange/header checks. Browser exchanges create Secure/HttpOnly/SameSite cookies; tests/local HTTP explicitly disable `Secure`. Admin review, exports, and deployment registration all require admin authorization.

This is intentionally lightweight. The application trusts anyone with a shared code, does not isolate one tester's guessed session UUID from another tester, and has no rate limit. Use high-entropy codes, HTTPS, Cloud Run/IAP or network controls, rotation, database least privilege, and suitable retention for any higher-risk deployment.

Provider response content is untrusted. The chat UI assigns it through DOM `textContent`; Jinja review autoescaping remains enabled. Provider response bodies are not copied into status-error messages. Secrets are absent from frontend assets, OpenAPI defaults, database deployments, logs by design, and exports.

## Reliability and operational behavior

The pending turn is committed before I/O, so a handled timeout/network/provider/malformed-response/application error can be finalized. A database partial unique index permits only one pending turn per session, closing cross-instance races before a second provider call; completed and failed turns remain eligible history. There are no generation retries. Cloud Run health checking uses `/healthz`; database readiness is operationally validated by migration and a session smoke test. Structured request logging is left to the platform for MVP, while provider request IDs remain in the database.

Migrations run as a one-off release step. They must not run in every horizontally scaled container. Cloud SQL backups are enabled in Terraform; deletion protection defaults on.

## Export contract

JSONL emits one complete turn per line, nested under `session`, `model`, `turn`, and `feedback`. CSV emits one turn per row and joins multiple feedback values with `|`. Both retain deployment ID/provider/model/version, full conversation text per turn, timestamps, metrics, failures, feedback, and provider request ID. Export does not imply consent or suitability for training.

## Extension points

- Add SSE by implementing provider token events and a streaming route while preserving the same pending/finalization guarantees.
- Add A/B/n assignment solely at session creation, recording assignment policy metadata.
- Add OIDC/IAP identities and owner roles without changing provider/history logic.
- Add aggregate dashboards as read-only queries over recorded timestamps and deployment IDs.

## Known risks

Shared credentials and public ingress are MVP controls. Browser local storage can be cleared, losing only the pointer—not durable records. Client metadata (including user-agent) may itself be identifying and needs a retention policy. External model APIs may log prompts independently. Simultaneous sends against one session can contend on unique turn numbering. Cloud Run request deadlines and remote model cold starts must be configured and measured in production.
