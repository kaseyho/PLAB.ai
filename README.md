# PLAB.ai — Airbus Challenge #2

A working airport coordination simulator: independent ATC, Pilot and Ground agents
negotiate a checked arrival plan, and a duty manager approves its application.
The organizer's workbook supplies synthetic operational limits.

**[Open the public demo](https://plab-ai-alpha.vercel.app/)** ·
**[Judge guide: requirements, rubric and evidence](https://plab-ai-alpha.vercel.app/static/judges.html)**

The judge guide maps every challenge requirement and all six weighted criteria to
observable features, saved examples and measured results. It separates live-model
consensus from contract checks, and includes a three-minute presentation route.

The app puts the three highlighted rubric strengths in the workflow itself:

- **Problem realism (20%):** a named airport duty-manager job, the current
  phone/manual-update process, its fuel/overtime/resource risks, and source limits.
- **Technical soundness (25%):** an explicit Python/LLM/human responsibility
  split, actual execution counts, and a same-input B0/B1 comparison before review.
  Change the simulated fuel quantity in Step 1 to exercise a new versioned input;
  an unknown quantity blocks planning and insufficient fuel can remove every option.
- **Honest evaluation (20%):** report-derived denominators, latency, all retained
  cases, baseline deltas and a recorded failure with a cause and next step. The
  initial 1/6 live consensus result remains distinct from 12/12 contract cases.

Fuel edits use the existing scenario-revision API and invalidate pending plans.
They retain synthetic provenance and cannot edit workbook or reserve policy.
Evaluation metrics describe the frozen report, not edited inputs or a new prompt.

## Public judge deployment

The frontend, Python API and agent execution run on Vercel Hobby, with a connected
Neon Free Postgres database for persistent state. Provider credentials remain
server-only environment variables. Keep the browser tab open during negotiation;
the bounded execution request streams until the agents finish.
Judges receive an anonymous browser session automatically: no API key or access
code is required, and each visitor has separate scenario/resource copies.
Human approval remains mandatory. Live-model failures still use the checked fallback.
Completed runs and human decisions survive function restarts. The previous
Render deployment remains available as a fallback.

Public mode permits ten runs per visitor per UTC day, 100 runs across the service
per UTC day, and two simultaneous negotiations. Saved examples remain available.
The service downloads the organizer CSV from the repository's data release and
verifies its SHA-256 before startup. See [deployment instructions](docs/deployment.md).

## Start the MVP

Requires Python 3.12 and uv. Keep the organizer's filtered flights.csv in data/.
The small operational workbook is included; the flight CSV remains local.

```sh
uv sync --locked --dev
cp -n .env.example .env
uv run python -m app.prepare
uv run python -m app.demo
```

Set SOCLAAS_API_KEY and SOCLAAS_MODEL in .env, or the OPENAI_* equivalents.
The demo prints a temporary operator access code when PLAB_SESSION_TOKEN is unset.
Alternatively set it to at least 16 random characters in .env. Keep credentials out of Git.

Open **http://127.0.0.1:8000**, enter that access code, select D01, and click
**Start negotiation**. Review the plan, messages, checks and B0/B1 comparisons. Enter a
reason and approve or reject. Fallback also needs the acknowledgement.
Each launch uses a fresh SQLite database under artifacts/demos/.

For an offline presentation with the real planner and review gate:

```sh
uv run python -m app.demo --offline
# Show a third-turn revision with explicitly labelled fixture agents:
uv run python -m app.demo --offline --third-turn
```

The UI always identifies fixture agents. Eight scripted read-only samples are available,
including third-turn agreement, missing data, no feasible plan, fallback,
rejection, stale approval and applied simulation.
Five additional read-only recordings show the actual roles-v6 live failures.
The negotiation section explains the recorded cause, each role's response, and
any non-accepting agent's stated reason. Timeouts and cancelled peer requests
are distinguished from rejection. Recorded live cases are labelled historical;
opening them makes no model calls.

## Implemented behavior

- One structured message per role per turn, at most **three turns / nine requests**,
  and no SDK retries. Valid dissent selects an untried common plan for fresh votes.
- **45 seconds per run**, **12 seconds per request**, **five seconds for fallback**.
  Fallback makes zero model calls. Invalid or missing inputs never become consensus.
- Up to 124 candidate combinations and 15 deterministic checks. Workbook rules
  cover airport inventory/capacity, wake times, reserve plus contingency fuel,
  flight duty, ground overtime, turnaround and gate notice.
- Human approval bound to the exact plan, versions and expiry. Application
  atomically reserves runway, gate, crew and equipment. Changed sources or stale
  state block approval; rejection/cancellation do not apply a plan.
- Persisted messages, requests, counters, checks and hash-chained audit events.
  Download the full trace from the dashboard.
- B0 earliest-feasible and B1 joint-cost baselines over the same full catalog.

## Inputs and scope

The delivered CSV was verified as **358,774 eligible 2015 rows**, SHA-256
`bfb7fc2112a1a98b748fa1c5bbba57f6f8f14acbd6419ea4cf84f4470ed5d79e`.
The workbook SHA-256 is
`f889ae9e012c36cbbf95a5b0b70a0ae81c1fa9090f07be5f9d0db81520e78211`.
Scenarios retain identity, ordinal, both hashes and source cells.
AA1057 DFW–MIA is now record **47**, rather than 71 in the legacy file.

One runway and two candidate spare gates form a declared subset of airport totals.
Traffic, occupancy, routes, aircraft type, fuel state, report times and equipment
are synthetic scenario inputs. The departure wake-time table is a **declared
arrival-simulation proxy**; nautical-mile spacing is retained but not simulated.
Filtered DOT rows are never treated as a full queue or an aircraft rotation.
This prototype is not operational flight-control software.

Default scenarios are in scenarios/organizer/. scenarios/D01.json remains the
legacy arithmetic regression fixture and is not loaded by the submission app.
See [the demo guide](docs/submission.md) for presentation steps and limitations.

## Verification and optional evaluations

```sh
uv run ruff check app examples tests
uv run ruff format --check app examples tests
uv run mypy
uv run pytest -q
node --check app/static/app.js
# Optional; not required to run the MVP:
uv run python -m app.evaluate
uv run python -m app.evaluate --live
```

Live evaluation starts at most 54 requests for six nominal holdout cases.
Fault cases use labelled fixtures. Reports retain failures and distinguish
contract checks from model consensus. Results appear at /api/evaluations/latest.
Traces, databases, local credentials and generated reports stay under ignored paths.

For a persistent service instead of an isolated demo:

```sh
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

/health reports input/provider setup; /docs exposes the API. Mutations require the
operator bearer token; starts/reviews also require Idempotency-Key.
See [the PRD](docs/technical_prd.md), [backend handoff](docs/backend_handoff.md) and
[three-developer plan](docs/developer_work_plan.md).
