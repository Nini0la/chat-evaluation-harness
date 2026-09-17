# Chat Evaluation Harness

A standalone, provider-neutral web application for evaluating conversational language models with invited testers. It pins each conversation to one model deployment, records raw responses and failures, captures latency/token metadata when available, collects optional feedback, and provides protected review/export tools.

This repository has **no dependency on Edge-IMCI or any other application**.

## Features

- Responsive open-ended chat UI with refresh restoration and **New Chat**
- Short privacy/evaluation notice, loading/error states, thumbs feedback, and problem reports
- Durable sessions, turns, failures, feedback, deployment metadata, and performance measurements
- Session-level deployment pinning (model changes never alter an existing conversation)
- Provider interface with local mock and configurable HTTP/Modal-compatible adapters
- Shared tester access code plus a separate admin code; provider secrets stay server-side
- Protected conversation review and JSONL/CSV exports
- PostgreSQL/Alembic for production; SQLite works for development and tests
- Docker, Cloud Build, and low-cost Terraform baseline for Cloud Run/Cloud SQL/Secret Manager

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for design details and data semantics.

## Local setup

Requires Python 3.11+.

```bash
cp .env.example .env
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
alembic upgrade head
chat-eval register-deployment \
  --provider mock --model-id mock-chat --model-version local-v1 --activate
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. Use the value of `TESTER_ACCESS_CODE`. For local development set `COOKIE_SECURE=false`; production must use `true`.

If standard-library `venv` is unavailable, `uv venv --seed .venv` followed by `uv pip install --python .venv/bin/python -e '.[dev]'` is equivalent.

## Configuration

All configuration is server-side and read from environment variables (or local `.env`).

| Variable | Purpose | Default |
|---|---|---|
| `ENVIRONMENT` | Runtime mode; every value except `development` enables strict access-code validation | `development` |
| `DATABASE_URL` | SQLAlchemy URL; use `postgresql+psycopg://...` in production | `sqlite:///./chat_eval.db` |
| `TESTER_ACCESS_CODE` | Shared invitation code; minimum 16 characters outside development | insecure development value |
| `ADMIN_ACCESS_CODE` | Separate review/export code; minimum 16 characters outside development | insecure development value |
| `COOKIE_SECURE` | Restrict auth cookies to HTTPS | `true` |
| `MODEL_PROVIDER` | Bootstrap/provider convention (`mock`, `http`, `modal`) | `mock` |
| `MODEL_ENDPOINT` | Fallback remote generation URL | empty |
| `MODEL_API_KEY` | Bearer token used only by the backend | empty |
| `MODEL_ID`, `MODEL_VERSION` | Deployment naming defaults/conventions | development values |
| `MODEL_TIMEOUT_SECONDS` | Remote request timeout | `60` |

Development permits the documented insecure access-code defaults for convenience. Every other
`ENVIRONMENT` fails configuration/startup validation when either access code is blank, still set to
a built-in or `.env.example` placeholder, shorter than 16 characters, or equal to the other code.
Set distinct, random values of at least 16 characters before running in staging or production.

Deployment rows contain endpoint references and non-secret generation settings. Never put tokens in `configuration_json` or `endpoint_reference`.

## Database and migrations

Apply migrations before starting a release:

```bash
alembic upgrade head
alembic current
```

Create migrations after model changes with `alembic revision --autogenerate -m 'description'`, inspect the generated file, then test upgrade and downgrade on a disposable database. The automated checks exercise a SQLite upgrade/downgrade round trip and assert PostgreSQL's generated downgrade SQL, including explicit removal of the `turnstatus` enum. A live disposable PostgreSQL round trip remains a deployment check when no PostgreSQL service is available locally; do not infer it from the SQL-generation test.

Core tables are `model_deployments`, `sessions`, `turns`, and `feedback`. See the architecture document for fields and relationships.

## Models and deployment switching

Register the initial mock model:

```bash
chat-eval register-deployment --provider mock --model-id mock-chat --model-version v1 --activate
```

Register and activate a remote model:

```bash
chat-eval register-deployment \
  --provider http \
  --model-id my-model \
  --model-version 2026-09-17 \
  --endpoint https://MODEL-SERVICE/generate \
  --configuration '{"temperature":0.2,"max_tokens":512}' \
  --activate
```

`--activate` atomically deactivates the prior deployment. Existing sessions retain their old `model_deployment_id`; only newly created sessions receive the new active deployment. `chat-eval list-deployments` shows the registry. The protected `POST /admin/deployments` API offers the same registration workflow.

## Provider HTTP contract and Modal

The HTTP adapter sends:

```json
{
  "model": "my-model",
  "model_version": "2026-09-17",
  "messages": [{"role": "user", "content": "Hello"}],
  "configuration": {"temperature": 0.2}
}
```

It adds `Authorization: Bearer <MODEL_API_KEY>` when configured. The expected response is:

```json
{
  "text": "Exact model output",
  "request_id": "provider-request-id",
  "usage": {"input_tokens": 10, "output_tokens": 4},
  "timing": {"time_to_first_token_ms": 120, "inference_ms": 450},
  "metadata": {"region": "optional non-secret diagnostics"}
}
```

Only `text` is required. Missing metrics remain null. To connect Modal later: expose a protected HTTPS function matching this contract, put its URL in a new deployment, store its token as `MODEL_API_KEY` in Secret Manager, register the deployment as `modal` or `http`, run a smoke chat, then activate it. No UI rebuild is needed.

## Review and export

Use an admin header for API/automation:

```bash
curl -H "X-Admin-Code: $ADMIN_ACCESS_CODE" http://localhost:8000/admin
curl -H "X-Admin-Code: $ADMIN_ACCESS_CODE" \
  'http://localhost:8000/admin/export?format=jsonl' -o interactions.jsonl
```

Or exchange the admin code at `POST /admin/access` for an HttpOnly cookie, then visit `/admin`. CLI export is also available:

```bash
chat-eval export --format csv --output interactions.csv
```

Exports are evaluation data, not automatically converted into training data. Handle them as sensitive research data. JSONL preserves exact stored values. CSV prefixes cells whose first non-whitespace character is `=`, `+`, `-`, or `@` with an apostrophe to prevent spreadsheet formula execution.

## Tests and quality checks

```bash
pytest -q
ruff check .
ruff format --check .
alembic upgrade head
python -m build
```

Tests cover session creation and new chats, session pinning, ordered history, exact response/failure/feedback persistence, latency math, providers and remote errors, browser secret exposure, access separation, review/export, and deployment switching.

## Docker

```bash
docker build -t chat-evaluation-harness .
docker volume create chat-eval-data
docker run --rm -v chat-eval-data:/app/data chat-evaluation-harness alembic upgrade head
docker run --rm -v chat-eval-data:/app/data chat-evaluation-harness \
  chat-eval register-deployment \
  --provider mock --model-id mock-chat --model-version local-v1 --activate
docker run --rm -p 8080:8080 -v chat-eval-data:/app/data chat-evaluation-harness
```

The image defaults SQLite to `/app/data/chat_eval.db`; `/app/data` is writable by the unprivileged
`app` user and the named volume keeps the database across commands. To use `--env-file .env`, either
remove its local `DATABASE_URL` line or override it afterward with
`-e DATABASE_URL=sqlite:////app/data/chat_eval.db`. Supply non-default access codes when setting
`ENVIRONMENT` to anything other than `development`.

Run `alembic upgrade head` as a release job, not concurrently in every web instance.

## GCP deployment

The production shape is Cloud Run → remote model endpoint and Cloud Run → Cloud SQL PostgreSQL. Secret Manager supplies database/access credentials; stdout/stderr flow to Cloud Logging.

1. Select a new GCP project and enable billing. Do not reuse or modify unrelated applications.
2. Create an Artifact Registry repository and build the image:
   ```bash
   gcloud artifacts repositories create chat-evaluation-harness \
     --repository-format=docker --location=europe-west1
   gcloud builds submit --config cloudbuild.yaml \
     --substitutions=_REGION=europe-west1,_REPOSITORY=chat-evaluation-harness
   ```
3. Copy `infra/terraform.tfvars.example`, set the immutable image URL/tag and project, then deploy the mock-backed service without a model key:
   ```bash
   cd infra
   terraform init
   terraform plan
   terraform apply
   ```
   Terraform provisions a small zonal PostgreSQL instance, database/user, generated DB/tester/admin secrets, service account/IAM, and a Cloud Run service with min instances 0 and max 2. It creates the `chat-eval-model-api-key` secret container but no version. Because `model_api_key_secret_version` defaults to `null`, `MODEL_API_KEY` is omitted and mock deployment continues to work.
4. Run migrations using Cloud SQL Auth Proxy locally or a one-off Cloud Run Job with the same image, Cloud SQL volume, service account, and `DATABASE_URL`, overriding the command to `alembic upgrade head`.
5. Register the initial `mock` deployment against production using a similarly protected one-off job or the admin API.
6. To configure a protected remote model, add the secret value directly with `gcloud`; never put it in a `.tfvars` file, a `TF_VAR_...` environment variable, or a Terraform command:
   ```bash
   PROJECT_ID="your-gcp-project"
   read -rsp 'Model API key: ' MODEL_API_KEY && printf '\n'
   VERSION_RESOURCE="$(printf '%s' "$MODEL_API_KEY" | \
     gcloud secrets versions add chat-eval-model-api-key \
       --project="$PROJECT_ID" --data-file=- --format='value(name)')"
   unset MODEL_API_KEY
   MODEL_API_KEY_VERSION="${VERSION_RESOURCE##*/}"
   terraform apply -var="model_api_key_secret_version=${MODEL_API_KEY_VERSION}"
   ```
   The Terraform input is only the non-secret numeric version identifier. After the new Cloud Run revision is healthy, register and activate the remote deployment.
7. Rotate the provider key by adding a new version and pointing Cloud Run at it before disabling the old version:
   ```bash
   OLD_VERSION="$MODEL_API_KEY_VERSION"
   read -rsp 'New model API key: ' MODEL_API_KEY && printf '\n'
   VERSION_RESOURCE="$(printf '%s' "$MODEL_API_KEY" | \
     gcloud secrets versions add chat-eval-model-api-key \
       --project="$PROJECT_ID" --data-file=- --format='value(name)')"
   unset MODEL_API_KEY
   MODEL_API_KEY_VERSION="${VERSION_RESOURCE##*/}"
   terraform apply -var="model_api_key_secret_version=${MODEL_API_KEY_VERSION}"
   curl --fail "$(terraform output -raw cloud_run_url)/healthz"
   gcloud secrets versions disable "$OLD_VERSION" \
     --secret=chat-eval-model-api-key --project="$PROJECT_ID"
   ```
   Persist only `model_api_key_secret_version = "VERSION_NUMBER"` if you want the selected version in a protected `.tfvars` file; the key value remains exclusively in Secret Manager.

Cloud Run grants `roles/run.invoker` to `allUsers`, so invited testers can open the output URL directly. For this MVP, the shared tester code enforced by the application is the access gate. It is intentionally lightweight; remove the public binding and add IAP or another perimeter control before using this baseline for higher-risk or broadly distributed studies.

Cloud SQL is the principal cost. `db-f1-micro`, 10 GB, zonal placement, Cloud Run scale-to-zero, and no warm model assumption keep the initial footprint small. Confirm current regional availability and pricing before applying.

## Privacy and security

- Do not solicit PII or secrets. Every interaction and client metadata are logged.
- Shared codes are appropriate only for a small invited MVP; rotate them and combine them with Cloud Run/IAP or ingress controls for higher-risk studies.
- Cookies are HttpOnly, SameSite=Strict, and Secure by default. Codes sent as headers can appear in client tooling; prefer cookies for browsers.
- Provider keys never enter templates, static JavaScript, API schemas, deployment rows, or exports.
- Exports, database backups, and logs require retention/access policies outside this application.

## Known limitations

- Responses are request/response today; the UI exposes a loading state and the provider interface has a stream fallback, but token-by-token SSE is not implemented.
- One shared tester code provides no individual identity, revocation, quotas, or abuse controls.
- A pending turn gates another send in the same session with HTTP 409 rather than allowing generation with divergent context; completed and failed turns do not block later sends.
- No automatic retry is performed, intentionally avoiding unrecorded alternate generations.
- Provider-supplied timing is trusted only when supplied and otherwise remains null; application total duration is measured locally.
- Terraform is a baseline and intentionally does not configure domains, IAP, VPC-only networking, alerting, retention, or organization policy.

## License

Add the public repository's chosen license before publication.
