# Handoff: Airbus Challenge #2 — Air traffic decisions

Updated 31 October 2026 for the organizer clarification. The [problem statement](docs/problem_statement.md) and [update record](docs/organizer_updates.md) are the current source of challenge requirements; the [technical PRD](docs/technical_prd.md) defines the build.

## Product and required behavior

The user is an airport operations duty manager coordinating one disrupted arrival. Three independent agents represent ATC, Pilot and Ground. They communicate structured preferences and concessions, vote on an exact plan, and expose the deterministic evidence to a human reviewer.

**Each turn contains one structured message from each of the three agents. Reach consensus within three turns, otherwise deterministic fallback decides.** That permits at most nine agent generation requests. The implemented profile uses two turns/six requests: offers, then same-plan votes. Its shorter bound is explicit; no third turn is currently implemented. Missing data, invalid output or a deadline can stop negotiation earlier.

Fallback searches the checked full catalog with zero model calls. If it cannot validate a plan, it exposes blockers and a non-executable result. Neither consensus nor fallback changes resources until a human approves the exact current plan. Rejection, cancellation, expired/stale plans and failed checks preserve the schedule.

## Data and operational limits

| Input | Current meaning and status |
|---|---|
| Organizer's updated `flights.csv` | Reported 358,774 disrupted-flight records from 2015 with `WEATHER_DELAY > 15 OR LATE_AIRCRAFT_DELAY > 20`; independently profile the delivered file |
| Local legacy `data/flights.csv` | Earlier 5,819,079-row file, 592,406,591 bytes; 376,125 delay-eligible rows and 358,774 lookup-resolved eligible rows in the earlier inspection |
| `challenge2_operational_constraints.xlsx` | Organizer-supplied synthetic airport gates, runways, movements/hour, turnaround, wake separation, fuel and crew rules; not available for inspection during this documentation update |
| Cited real figures | Accepted replacement values when publisher, document/URL, revision, section, units and applicability are recorded |

A matching cohort size does not prove that the old file and new release are identical. D01's old record 71 is valid only with the legacy file hash and AA1057/date/airport identity. Rebind and verify locators when adopting a filtered release.

The DOT data contains no gates, runways, fuel or crew. The filtered cohort also cannot establish the complete arrival queue, total airport movements or the next aircraft leg. Source those from verified additional data or explicit simulation. Workbook rules and airport totals do not establish current occupancy, aircraft fuel state or crew availability by themselves.

Import workbook facts into versioned normalized scenarios only after inspecting their exact sheets/cells and units. The service currently loads JSON scenarios and has no automatic workbook importer. Existing D01 constants are illustrative; they have not been matched to the workbook. Keep unavailable facts unknown and preserve explicit synthetic labels.

The one-runway/two-gate simulator is a bounded subset of an airport, not its claimed full inventory. Validate it against sourced airport totals, state background-load assumptions, and keep the finite search limit visible.

## Current implementation

The main branch contains the FastAPI service, SQLite store, deterministic planner/fallback, three independently invoked role agents, a duty-desk UI, atomic human review and trace exports. Setup and commands are in [README](README.md); exact interfaces and retained live smoke evidence are in [the backend handoff](docs/backend_handoff.md).

The last combined code verification passed 176 offline tests, Ruff, strict mypy and JavaScript syntax checks. Browser checks covered approvals, rejection, refresh, authentication and seven labelled fixture samples. These are development checks; the frozen evaluation suite, B1 comparator and measured report remain pending. Earlier smokes used the legacy dataset and illustrative overlay, not the new workbook.

Simulated application persists runway and gate reservations. Crew/equipment availability is checked from scenario facts but is not persistently reserved.

## Three owners and immediate follow-up

| Developer | Owns | Organizer-update work |
|---|---|---|
| 1 / A | Dashboard, evidence, human review and simulator application | Explain actual turns/profile, expose workbook or cited provenance, retain all review guards |
| 2 / B | Contracts, API/store, provider calls, all three agents and coordination | Enforce structured role-message accounting and version bindings; keep the tested two-turn profile or explicitly version/test any third-turn extension |
| 3 / C | Data, scenarios, rules, catalog, fallback, B0/B1 and evals | Obtain/profile updated files, migrate source locators, map workbook values and applicability, freeze/rerun paired evaluations |

The [four-hour plan](docs/developer_work_plan.md) gives detailed ownership. All three agents remain Developer B's responsibility; the developers are not split by aviation role.

## Judging evidence

| Criterion | Weight | Evidence |
|---|---:|---|
| Problem realism | 20% | Named workflow, credible coordination burden and input limits |
| Technical soundness | 25% | Independent role calls, explicit turns, checked constraints and fallback |
| Honest evaluation | 20% | Frozen cases, actual counts, B0/B1 comparisons and a visible failure |
| Trust and traceability | 20% | Historical/workbook/cited/scenario provenance, exact-plan votes and human authority |
| Impact | 10% | Paired modeled outcomes, including regressions and assumptions |
| Time discipline | 5% | Actual submission confirmation and a focused presentation within three minutes |

Use the 12-case minimum in the PRD before optional broader sampling. Report the exact evaluated count and retain failures. Synthetic workbook figures remain synthetic; neither 358,774 source rows nor 176 unit tests constitute a measured agent benchmark. Do not claim the filtered data establishes traffic-wide or aircraft-rotation statistics.

## User context

The project owner's RSAF 508 SQN experience motivates the communication and runway-maintenance problem. Use it as personal workflow context, not as evidence for commercial airport capacities, repair times or safety rules. Keep the demo on the challenge's historical commercial-flight data and declared synthetic operations. Details are in [user context](docs/user_context.md).
