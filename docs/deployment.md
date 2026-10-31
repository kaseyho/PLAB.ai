# Public judge deployment

## Vercel Hobby with Neon Free

Public judge URL: **https://plab-ai-alpha.vercel.app/**.

The frontend, FastAPI API and bounded agent execution deploy together as one
Vercel project. Persistent data lives in the connected Neon Free Postgres
database. Judges use the public Vercel address and need no API key. Both services
have free usage limits; the database was provisioned on `free_v3`, with no paid
upgrade. Vercel is serverless and can have cold starts, but does not require
waking a Render web service.

`app/vercel.py` exports the FastAPI application. `pyproject.toml` declares that
entrypoint and the build command, which downloads and verifies the organizer CSV.
`vercel.json` places the function in Singapore and permits a 120-second request;
the coordinator still enforces the PRD's 45-second run budget, three turns and
nine role requests. Public static assets can be served from Vercel's CDN.

`.vercelignore` excludes local environment files, databases, development caches,
Git history and unrelated artifacts from deployments. Credentials are runtime
variables, never browser configuration or build arguments.

### Persistent state and review

Set `DATABASE_URL` using the Neon integration. SQLite remains available for local
and Render use; Vercel refuses to start without shared Postgres storage. The
Postgres adapter serializes short transactions across instances, preserving
idempotency, audit chains, admission budgets and atomic human review. Starting a
new function instance recovers only expired work, rather than interrupting runs
owned by another instance.

A new run first returns its persisted ID. In Vercel mode the browser then opens
`POST /api/runs/{id}/execute`, which holds a streamed response open until the
negotiation finishes. A persisted execution claim permits one executor per run.
The UI continues polling the shared state and events for actual progress.

**Keep the tab open during negotiation.** A disconnected execution request
cancels its task; an expired unfinished run is reported as interrupted on its next
read. No detached job or automatic model-call retry is assumed. Completed runs
and pending human review persist independently of the execution request.

### Runtime configuration

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | Injected by the Neon Free integration |
| `PLAB_PUBLIC_DEMO` | `true` |
| `PLAB_SESSION_TOKEN` | Server-only random secret, at least 32 characters |
| `PLAB_DAILY_RUN_LIMIT` | `100` |
| `PLAB_EVALUATION_PATH` | `evals/results/initial-live-holdout.json` |
| `SOCLAAS_API_KEY` | Team key, stored as a sensitive variable |
| `SOCLAAS_MODEL` | `qwen3.8:27b` |
| `SOCLAAS_BASE_URL` | `https://soclaas-api.comp.nus.edu.sg/v1` |
| `SOCLAAS_API_MODE` | `chat_completions` |

Vercel supplies `VERCEL=1`, which enables request-bound execution. The app uses
`VERCEL_PROJECT_PRODUCTION_URL` for the trusted production origin and `VERCEL_URL`
for preview deployments. Set `PLAB_PUBLIC_ORIGIN` only to override this with the
exact HTTPS address judges visit; never deploy a localhost override.

### Deploy and verify

Deploy from the linked repository using `vercel deploy --prod`. Keep the Neon
resource on its Free plan and the Vercel project on Hobby. Preserve the team's
server-only provider key and existing daily/per-visitor run limits.

Before changing the submission URL, verify health, six loaded flight scenarios,
the historical evaluation report, static assets, a live negotiation, its audit
chain and human review from a separate request. A model timeout remains a visible
fallback; it must never be reported as agent consensus. Existing Render records
are not copied into the new database; the historical evaluation fixtures ship
with the app.

References: [FastAPI on Vercel](https://vercel.com/docs/frameworks/backend/fastapi),
[function limits](https://vercel.com/docs/functions/limitations),
[Postgres integrations](https://vercel.com/docs/postgres).

## Render fallback

Use the team repository's main branch on a Render Python web service in Singapore.
The checked-in `render.yaml` describes the free demo plan selected by the team.
It has no persistent disk. Idle wake-up can take about a minute, and SQLite state
is discarded when the service sleeps, restarts or redeploys.

Build: `uv sync --locked --no-dev && uv run python -m app.download_data`

Start: `uv run python -m app.serve`

Health check: `/health`

The flight-data release URL is fixed in `app/download_data.py`. Its SHA-256 must
match the organizer data before the file is accepted. The workbook and normalized
scenarios are included in Git. Provider keys and local `.env` files are not.

Set the runtime variables from `render.yaml`. Put the team's provider key in
`SOCLAAS_API_KEY` using Render's environment editor. Generate a separate random
`PLAB_SESSION_TOKEN` with at least 32 characters. The app uses this only on the
server to sign short-lived, HttpOnly judge cookies. `RENDER_EXTERNAL_URL` supplies
the trusted browser origin automatically; do not copy a localhost origin into Render.

Run one service instance and one Uvicorn worker. The coordinator's background tasks
and SQLite transaction boundaries assume this deployment. The free deployment
uses `PLAB_DB_PATH=/tmp/plab.sqlite3`. To retain traces across restarts later,
upgrade to a paid instance with a persistent disk, mount it at `/var/data`, and
set `PLAB_DB_PATH=/var/data/plab.sqlite3`. That upgrade requires hosting-budget approval.

With `PLAB_PUBLIC_DEMO=true`, the browser opens a judge session automatically.
Scenario and resource IDs belong to that session; read, cancel, review and export
routes reject other sessions' IDs. Approvals still check the exact plan, expiry,
source files, versions and deterministic rules before any simulated booking.

The service permits two active negotiations, ten starts per visitor per UTC day,
and 100 starts across the app per UTC day. Idempotent retries do not consume another
run. `PLAB_DAILY_RUN_LIMIT` can lower the global limit. Server-side credentials are
never sent in browser configuration, API responses, source files or trace exports.

Verify the production page with a new browser session: choose a flight, start a
negotiation, inspect the outcome, and approve or reject with a reason. Check a
second session sees separate resources. Live failures must remain labelled fallback;
the read-only examples remain explicitly labelled as fixture-agent records.
