# Public judge deployment

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
