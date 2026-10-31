# PLAB.ai

**Three AI agents help an airport duty manager compare arrival plans.**

Arrival planning with independent agents, deterministic checks and human approval.

[Open PLAB.ai](https://plab-ai-alpha.vercel.app/)

## Root cause → problem → solution

- **Root cause:** Air traffic control, pilots and ground crews plan separately, coordinating disruptions through phone calls and manual updates.
- **Problem:** Their plans can clash, adding delays, fuel use and crew overtime.
- **Solution:** Three AI agents compare options, Python rules check the constraints, and a human reviews the recommendation.

PLAB.ai focuses on **when a delayed flight can land and which gate it can use**.

## Try it

Open **[PLAB.ai](https://plab-ai-alpha.vercel.app/)**. No API key or access code is needed.

1. Choose a flight and click **Start negotiation**. Keep the tab open while it runs.
2. Inspect the agents' replies, the recommended plan and its checks.
3. Enter a reason and **approve or reject** the plan.

The **Mode** menu includes saved agreements, failures and missing-data examples.
You can also change the scenario fuel quantity to try a different input.

## How it works

| Agent | Focus |
| --- | --- |
| Air traffic control (ATC) | Runways, aircraft spacing, taxi routes and airport capacity |
| Pilot | Fuel reserves and flight-crew duty limits |
| Ground operations | Gates, handling crews, equipment and turnaround |

Python builds candidate plans and runs **15 deterministic checks**. Each agent
makes its own model request and sends one structured message per turn:

- **Turn 1:** Offer preferred options and acceptable compromises.
- **Turn 2:** Vote on one shared proposal.
- **Turn 3, if needed:** Revise the proposal and collect fresh votes.

Consensus requires **all three agents to accept the same complete plan**.
The limit is **three turns, nine requests and 45 seconds**, with 12 seconds per request.

Without agreement, fixed rules choose a checked fallback using no extra AI calls.
The app explains the cause, including disagreements, timeouts or invalid replies.
Missing critical data or no feasible option can leave the run without a plan.

**Human approval is always required**, including for fallback. Approval rechecks
current resources before saving the plan and its runway, gate, crew and equipment
allocations together in the workspace. Stale or expired plans cannot be applied.

## Data and limits

- **Real historical flights:** The supplied `flights.csv` contains **358,774 records from 2015**, filtered by `WEATHER_DELAY > 15 OR LATE_AIRCRAFT_DELAY > 20`.
- **Synthetic operational rules:** `data/challenge2_operational_constraints.xlsx` supplies airport capacity, gate/runway totals, turnaround, wake separation, fuel and crew rules. These facts are absent from the CSV.
- **Scenario inputs:** Traffic, occupancy, routes, fuel and crew availability are scenario inputs. We model one runway and two candidate gates; departure wake times serve as a declared proxy for arrival spacing.

Source hashes and workbook references remain attached to the evidence.
Approvals save planning records within PLAB.ai. The historical flight data and
synthetic operational inputs are labelled throughout the evidence.

## Evaluation: what worked and what failed

Plans are compared with two rule-based baselines using the same inputs:
**B0, the earliest feasible plan**, and **B1, a balanced-cost plan**.

The initial live evaluation on **31 October 2026**, using prompt `roles-v6`, recorded:

| Measure | Result |
| --- | --- |
| Live runs with agent consensus | **1 of 6**; five used fallback |
| System contract cases passed | **12 of 12**, including six scripted fault cases |

All six live results matched B0 on measured impacts. No improvement over B0 was shown.
These historical results do not measure the final prompt version or real airport savings.
Failures remain visible in the app.

[Evaluation report](evals/results/initial-live-holdout.json) ·
[Methods and limitations](docs/submission.md#evidence-retained)

## Tech stack and hosting

| Part | Technology |
| --- | --- |
| Interface | HTML, CSS, JavaScript and SVG animations |
| API, agents and rules | Python, FastAPI and a custom three-agent coordinator |
| Model access | Qwen through SoCLaaS; OpenAI-compatible provider support |
| Production hosting | Vercel Hobby for the interface, API and agent execution |
| Storage | Neon Free Postgres in production; SQLite for local demos |

API keys stay on the server. Saved runs and human decisions survive function restarts.
Vercel runs each negotiation inside an open streamed request. Both hosting plans
have usage limits. See [deployment details](docs/deployment.md).

## Run locally

Requires **Python 3.12** and **uv**. From the repository root:

```sh
uv sync --locked
cp -n .env.example .env
uv run python -m app.download_data
uv run python -m app.demo --offline
```

The download command verifies the flight CSV's hash. The workbook and prepared
scenarios are included in Git. Open **http://127.0.0.1:8000** and use the operator
code printed in the terminal, or your configured `PLAB_SESSION_TOKEN`.
Offline mode uses labelled scripted agents with real checks and human review.

For live agents, set `SOCLAAS_API_KEY` and `SOCLAAS_MODEL` in `.env` (or the
`OPENAI_*` equivalents), then run `uv run python -m app.demo`.
Use `--offline --third-turn` to demonstrate a scripted revised-plan vote.
Each local demo launch creates a fresh SQLite database. Keep credentials out of Git.

## More details

[Technical PRD](docs/technical_prd.md) ·
[Demo guide](docs/submission.md) ·
[Developer handoff](HANDOFF.md)
