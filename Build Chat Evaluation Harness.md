Build a standalone public GitHub project called:

`chat-evaluation-harness`

This project is a generic conversational model evaluation harness. It must not depend on, inspect, or import any Edge-IMCI application code.

The purpose is simple:

- give invited testers a link
- let them have open-ended conversations with a remotely hosted language model
- persist every conversation and model response
- measure useful latency/performance metrics
- collect optional tester feedback
- make the model endpoint replaceable so different models/fine-tunes can be evaluated later

The model itself will eventually be hosted on Modal, but the model endpoint is not necessarily ready yet. Build the application so that the model integration is cleanly configurable and can initially use a mock/stub endpoint if necessary.

## Architecture

Use:

```text
Tester browser
      ↓
GCP-hosted web app/API
      ↓
Remote model endpoint
      ↓
Model response

GCP backend
      ↓
PostgreSQL
```

Target infrastructure:

- Cloud Run for the web application/backend
- Cloud SQL PostgreSQL for durable experiment data
- Secret Manager for secrets
- Cloud Logging for normal operational logs

The remote model provider should be abstracted behind an adapter/interface. Modal will be the first real provider, but do not hard-code the application around Modal-specific assumptions.

## Core principle

The harness evaluates a model as a conversational system.

It does not need:

- structured-output extraction
- deterministic clinical logic
- JSON repair
- domain-specific workflows
- patient state
- Edge-IMCI code
- knowledge of how the model was trained

It only needs to send conversational history to a configured model endpoint and record what happens.

## Repository

Create the repository/application structure cleanly enough to be published publicly.

Do not commit:

- credentials
- service-account keys
- database passwords
- Modal tokens
- API keys
- private model weights
- interaction datasets
- production `.env` files

Create a `.env.example`.

Include a clear README.

## UI

Build a minimal, polished chat interface.

Required behaviour:

- open-ended chat
- visible conversation history
- text input
- Send button
- New Chat button
- loading/streaming state
- basic error state
- optional thumbs-up / thumbs-down after model responses
- optional “Report problem” text field or dialog

It should work well on both desktop and mobile.

Do not spend excessive effort on elaborate design.

## Sessions

A session represents one conversation.

When a new conversation begins:

- generate a UUID `session_id`
- resolve the currently active model deployment
- permanently associate that session with that model deployment
- store the session durably

A browser refresh should preserve the active conversation if practical.

Clicking `New Chat` creates a new session.

Do not switch model versions in the middle of an existing session.

This is important.

If model v1 was assigned when the conversation began, all subsequent turns in that session use v1 even if a newer model becomes active later.

## Conversation handling

For each user message:

1. persist/create the interaction attempt
2. reconstruct the conversation history for that session
3. send the appropriate history to the configured model endpoint
4. stream the response if supported
5. show the raw model response to the tester
6. persist the exact response shown
7. persist timing/performance metadata
8. persist failures rather than silently losing them

Do not modify or “improve” model responses before storing them.

## Database

Use PostgreSQL with proper migrations.

At minimum create the following logical entities.

### `model_deployments`

Fields should include approximately:

```text
id
provider
model_id
model_version
endpoint_reference
configuration_json
created_at
activated_at
deactivated_at
active
```

Do not store secrets in this table.

`configuration_json` can hold non-secret information such as:

- temperature
- top_p
- max_tokens
- chat template identifier
- quantization
- model revision
- adapter revision

where available.

### `sessions`

```text
id UUID
anonymous_tester_id nullable
model_deployment_id
created_at
ended_at nullable
client_metadata JSONB nullable
```

A persistent random browser-generated tester identifier is acceptable.

Do not require real names/accounts for the MVP.

### `turns`

Each user-message/model-response pair should store approximately:

```text
id UUID
session_id
turn_number

user_message
assistant_response nullable

request_started_at
inference_started_at nullable
first_token_at nullable
response_completed_at nullable

input_tokens nullable
output_tokens nullable

time_to_first_token_ms nullable
inference_latency_ms nullable
total_latency_ms nullable
tokens_per_second nullable

model_deployment_id

status
error_type nullable
error_message nullable

provider_request_id nullable

created_at
```

Use appropriate types and constraints rather than blindly reproducing this pseudocode.

### `feedback`

```text
id UUID
turn_id
rating nullable
failure_category nullable
comment nullable
created_at
```

Feedback should be optional.

Do not require feedback after every answer.

## Latency / performance instrumentation

Latency is part of the evaluation dataset.

Capture wherever technically possible:

- request start
- inference start
- first token received
- response completion
- total latency
- time to first token
- input tokens
- output tokens
- tokens/sec

If the remote provider cannot expose one of these metrics, leave it null rather than fabricating it.

Design the data so later analysis can calculate:

- median
- p90
- p95
- maximum
- distributions by model version

If possible, preserve enough provider/request information to investigate cold-start outliers later.

Do not pretend to know whether a request was cold or warm unless the serving infrastructure provides a trustworthy signal.

## Model provider abstraction

Create a clean interface such as:

```text
ModelProvider
    generate(...)
    stream(...)
```

or the equivalent appropriate to the chosen stack.

The rest of the application should not care whether the backing provider is:

- Modal
- a local development mock
- another API
- another deployment in the future

Provide at least:

1. a mock provider for local development/tests
2. a configurable HTTP remote-model provider suitable for later connecting to Modal

Expected configuration should be broadly equivalent to:

```text
MODEL_PROVIDER
MODEL_ENDPOINT
MODEL_API_KEY
MODEL_ID
MODEL_VERSION
```

Names can differ if the implementation has a better configuration design.

Never expose provider secrets to browser JavaScript.

The browser talks to the application backend.

The backend talks to the model provider.

## Model replacement

Make changing the active model straightforward.

The intended workflow is:

```text
current model
    ↓
new fine-tuned model
    ↓
register new model deployment
    ↓
mark it active
    ↓
new sessions use new model
    ↓
existing sessions remain pinned to old model
```

This should not require rebuilding the chat UI.

## Future A/B/n support

Do not build an elaborate experimentation platform now.

However, ensure the schema and provider abstraction can later support:

```text
model A
model B
model C
```

with model assignment performed once when a session is created.

Do not assign models independently on every turn.

## Tester feedback

For MVP support:

- thumbs up
- thumbs down
- optional text comment

Optionally support a small failure-category list such as:

```text
incorrect answer
hallucination
instruction-following failure
language-understanding failure
code-switching failure
unsafe response
over-refusal
repetition
irrelevant response
other
```

Do not force categorization.

## Review / export

Provide a simple protected owner/admin view or command-line/export workflow that allows the owner to inspect:

- sessions
- full conversations
- timestamps
- model version
- latency
- errors
- tester feedback

Support export to at least JSONL or CSV.

Exports must retain enough metadata to know exactly which model deployment generated each response.

Do not automatically convert interaction data into training data.

The interaction database is first and foremost an evaluation/failure-discovery dataset.

## Access control

This is intended for invited testers, not unrestricted public usage.

Implement lightweight protection suitable for an MVP.

A shared access code or similarly simple mechanism is acceptable.

Do not build a large identity/authentication system unless there is a compelling reason.

Protect any admin/review interface separately.

## Tester notice

Show a concise notice informing testers that:

- this is an experimental model
- their interactions are being logged for evaluation
- they should not submit sensitive or personally identifying information

Keep the notice short enough that people will actually read it.

## Reliability

A failed model request should still leave a useful record.

Preferred flow:

```text
create pending turn
↓
call model
↓
record response OR failure
↓
finalize turn
```

Record:

- timeout
- network failure
- provider failure
- malformed stream
- application exception where recoverable

Do not silently retry model generation if doing so could change the response without recording that a retry happened.

If retries are implemented, record attempts.

## GCP deployment

Prepare the application for:

- Cloud Run
- Cloud SQL PostgreSQL
- Secret Manager

Create deployment documentation and infrastructure configuration where appropriate.

Do not assume pre-existing application infrastructure.

Create only what this harness needs.

Do not inspect or modify unrelated GCP applications.

Keep costs low because usage will initially be small and intermittent.

## Tests

Add meaningful automated tests covering at least:

- session creation
- model pinning per session
- New Chat behaviour
- conversation-history reconstruction
- message ordering
- response persistence
- failure persistence
- feedback persistence
- latency calculations
- mock provider integration
- remote provider error handling
- secrets not exposed to frontend
- admin access protection

## Documentation

Create:

`README.md`

and:

`docs/ARCHITECTURE.md`

Document:

- architecture
- local setup
- environment variables
- database schema
- migrations
- local mock-model development
- Cloud Run deployment
- Cloud SQL setup
- Secret Manager setup
- how model providers work
- how to register/switch model deployments
- how session pinning works
- how to export interactions
- how to connect Modal later
- security/privacy considerations
- known limitations

## Implementation approach

First inspect the empty/new repository and choose a sensible stack.

Prefer a small maintainable implementation over unnecessary services or abstractions.

You are autonomous: make reasonable implementation decisions yourself.

Do not wait for the real fine-tuned model before building the application.

Use a mock provider so the entire system can be exercised end-to-end now.

At the end, report:

1. what you built
2. architectural decisions
3. files/components created
4. database schema
5. tests and results
6. what GCP resources are required
7. what secrets/configuration remain required
8. exactly what will need to happen later to connect the Modal model
9. any assumptions or unresolved risks