# Architecture

## Goals and boundaries

The harness evaluates a remote model as a conversational system. It sends ordered chat history, displays and stores the exact returned text, records operational evidence, and collects optional human feedback. It deliberately has no clinical workflow, structured-output repair, training-data pipeline, or dependency on Edge-IMCI.

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
- `app/models.py`: SQLAlchemy entities and invariants.
- `app/export.py`: complete per-turn JSONL records and analysis-friendly CSV flattening.
- `app/templates`, `app/static`: responsive chat/review UI. Model text is inserted with `textContent`, not interpreted as HTML.
- `migrations/`: authoritative schema evolution.
- `infra/`, `Dockerfile`, `cloudbuild.yaml`: production baseline.

## Request lifecycle

1. `POST /api/sessions` reads the single active deployment and creates a UUID session referencing it.
2. `POST /api/sessions/{id}/messages` creates and commits a pending turn before any model call.
3. Completed prior turns are ordered by `turn_number`; each contributes its user then assistant message. The current user message is appended.
4. The provider assigned by the session's deployment is called exactly once. No silent generation retry occurs.
5. On success, the exact provider `text`, IDs, token counts, optional metadata, timestamps, and derived metrics finalize the turn.
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

This is also the foundation for future A/B/n assignment: replace active-deployment selection with an assignment policy at session creation, without changing turn dispatch.

## Provider abstraction

`ModelProvider.generate(messages, deployment)` returns a `GenerationResult`; `stream` exists as a fallback interface. The application depends only on this contract. Provider errors carry stable categories (`timeout`, `network_failure`, `provider_failure`, `malformed_response`, `configuration_error`).

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
