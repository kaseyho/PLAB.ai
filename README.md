# PLAB.ai

Airport coordination simulator for the Airbus challenge: independent ATC, Pilot,
and Ground agents negotiate a checked plan, then a human reviews its simulated
application. This is a prototype, not an operational aviation system.

The requirements are in [the technical PRD](docs/technical_prd.md); ownership is in
[the three-developer work plan](docs/developer_work_plan.md). The
[organizer clarification](docs/organizer_updates.md) records the new flight release,
turn definition and operational workbook requirements.

One turn is one structured message from each of ATC, Pilot and Ground. The challenge
allows consensus within three turns (nine agent generation requests maximum), then
deterministic fallback. The implemented profile uses two turns/six requests, with
early fallback on failure or dissent. Fallback makes no model calls. Human approval
is required before simulated application in either case.

## Backend setup

Use Python 3.12 and [uv](https://docs.astral.sh/uv/). Dependencies are locked in
`uv.lock`.

```sh
uv sync --locked --dev
cp -n .env.example .env
```

Configure `.env` locally. Never commit credentials:

- `SOCLAAS_API_KEY`, `SOCLAAS_MODEL`, `SOCLAAS_BASE_URL`: the configured SoCLaaS
  provider. Use an active key and a supported model ID. The example uses
  `qwen3.8:27b`, verified on the backend conflict fixture.
- `SOCLAAS_API_MODE=chat_completions`: the verified gateway endpoint. The adapter
  requests reasoning disabled to reserve the output budget for the structured reply.
- Alternatively, leave `SOCLAAS_API_KEY` unset and configure `OPENAI_API_KEY`,
  `OPENAI_MODEL`, and optionally `OPENAI_BASE_URL`.
- `PLAB_SESSION_TOKEN`: a local operator token of at least 16 characters. Generate
  one with `openssl rand -hex 24` and put it in this field locally.
- `PLAB_REVIEWER_ID`, `PLAB_DB_PATH`, `PLAB_PUBLIC_ORIGIN`: local reviewer identity,
  SQLite location, and browser origin. Defaults bind the service to
  `http://127.0.0.1:8000`.
- Put the selected historical dataset at `data/flights.csv`, or set
  `PLAB_DATA_DIR` to its containing directory. The organizer reports a new release
  of **358,774 rows from 2015**, selected by `WEATHER_DELAY > 15 OR
  LATE_AIRCRAFT_DELAY > 20`. Profile the actual file and preserve its hash.
- The local CSV is still the earlier 592,406,591-byte full release. Current D01
  loads its record 71 (AA1057 DFW–MIA, 2015-01-01). Before switching to the filtered
  release, re-extract and verify that identity, its qualifying values, new ordinal
  and file hash in the scenario. Old ordinals do not transfer across releases.
- Obtain `challenge2_operational_constraints.xlsx` for synthetic airport gates,
  runways, movements/hour and turnaround, plus wake separation, fuel and crew
  rules; cited real figures are also accepted. Inspect exact cells, units and
  applicability before producing normalized versioned JSON scenarios. The workbook
  was not available for inspection during this documentation update. The service
  does not automatically import XLSX; current D01 constants remain illustrative.

Environment variables take precedence over `.env`. A configured SoCLaaS key takes
precedence over OpenAI configuration. The adapter uses the OpenAI SDK, with Chat
Completions for SoCLaaS and Responses for OpenAI. Both request a strict JSON schema,
a 500-token output cap, and zero retries; local schema and policy validation remain
mandatory. Set `<PROVIDER>_API_MODE` to `responses` or `chat_completions` to select
the endpoint explicitly. The endpoint never switches during a run. SoCLaaS
documents a [Responses API subset](https://soclaas.comp.nus.edu.sg/guides/api.html);
its responses did not consistently obey the requested schema in the initial smoke
tests. The exact contract is also included in the trusted role prompt. An unsupported
request becomes an explicit provider failure and deterministic fallback, never a
hidden retry.

## Run Developer B's slice now

```sh
# Offline protocol smoke: deterministic role responses and planner fixtures.
uv run python -m examples.backend_demo

# Real provider calls over the same explicitly labelled planner fixture.
uv run python -m examples.backend_demo --live

# Local fixture API for Developer A; add --live to use the configured provider.
uv run python -m examples.backend_demo --serve
```

Each command uses a fresh database under `artifacts/backend-smoke/`. Smoke runs
save their full trace and print the path. `planning_mode=FIXTURE` remains visible
even when `provider.mode=LIVE`. Fixture checks are test certificates, not an
aviation checker or evaluation baseline. The fixture review hook only demonstrates
atomic storage behavior. Nothing applies automatically.

To run the backend with the deterministic planner integrated from
[PR #1](https://github.com/kaseyho/PLAB.ai/pull/1):

```sh
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

Visit `/docs` for the API schema and `/health` for integration status. Mutations
require `Authorization: Bearer <PLAB_SESSION_TOKEN>`; starting a run and recording
a review also require `Idempotency-Key`. Poll run status and incremental events.
The API serves the duty desk at `/`. Select a scenario, enter the configured
operator token, and start a run. The token remains in that tab's memory. The
dashboard shows actual provider/planner modes, events, deterministic impacts,
checks and review expiry. Both approval and rejection require a reason. Explicit
`?sample=consensus` (or `fallback`, `no-feasible`, `insufficient-data`, `stale`,
`applied`, `rejected`) opens a labelled sample with actions disabled.
The `?fixture=` alias is also supported.

The default service uses the integrated checker and the existing illustrative D01
scenario. DOT data has no gates, runways, fuel or crew; the filtered disrupted-flight
cohort also does not establish a full traffic schedule or aircraft rotation. Use
inspected workbook/cited limits and explicit scenario state, not inferred operational
facts. The one-runway/two-gate slice must be declared as a subset of sourced airport
totals. Updating source facts/rules requires new versions and invalidates old reviews.

The default service loads the selected historical file. A missing CSV
appears in `/health` as `SOURCE_FILE_NOT_FOUND`; it never fabricates a historical
row. Human approval rechecks the current runway/gate reservations inside the
SQLite transaction before applying the simulated plan. Refreshing a scenario
retains committed reservations and invalidates old recommendations. Applied
bookings include the run ID, candidate ID and plan hash. This prototype reserves
runway and gate occupancy; crew and equipment checks use the scenario's availability
and do not yet create persistent reservations. Negotiation does not apply a plan.
See [the integration handoff](docs/backend_handoff.md) for
exact signatures, payloads, and acceptance evidence.

## Verify

```sh
uv run ruff check app examples tests
uv run ruff format --check app examples tests
uv run mypy
uv run pytest -q
```

The tests make no live model calls. The last merged code verification passed 176
offline tests; this documentation update does not claim workbook-backed tests or
new-release evaluation results. They cover the scenario hard rules, catalog
and fallback integration, coordination, provider requests, failure recovery,
persistence, authorization, and atomic approval. B0/B1 comparisons and the frozen
evaluation suite remain separate team gates. `/api/evaluations/latest` returns
`NOT_YET_EVALUATED` until C writes `artifacts/evals/latest.json`.

`data/` is ignored because the supplied flight CSV exceeds GitHub's file size
limit. Runtime databases, traces, reports, local environment files, and caches are
also ignored. Retain failures and review evidence before deliberately publishing
any generated artifact.
