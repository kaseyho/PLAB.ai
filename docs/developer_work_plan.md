# Four-hour work plan for three developers

This is the execution plan for [the technical PRD](technical_prd.md). Three developers work in parallel for four hours: approximately 12 developer-hours in total. Assign actual names to the three roles before starting.

The [organizer clarification](organizer_updates.md) now requires one structured
message per ATC/Pilot/Ground agent per turn, consensus within three turns and
otherwise deterministic fallback. The ceiling is three turns/nine requests;
the implemented profile uses two turns/six requests. The new historical release
is organizer-reported as 358,774 rows from 2015. Operational limits come from
`challenge2_operational_constraints.xlsx` (synthetic) or cited real figures.
The workbook is not yet inspected locally and the new release has not been
profiled here; these are explicit follow-up tasks, not completed integration.

The original four-hour clock and checklists below describe delivery ownership,
not a current completion inventory. See [the backend handoff](backend_handoff.md)
for existing implementation and verification.

The deliverable is one working flow: **qualifying flight → checked alternatives → three-agent negotiation → human review → simulated application → measured evaluation report**.

## 1. Assign these three owners

| Developer | Owns | Finished when |
|---|---|---|
| **Developer 1 / A: Interface and human control** | Browser dashboard, evidence display, approve/reject/cancel interactions, approval business logic, simulator application, demonstration | A reviewer can inspect a real recommendation, approve it exactly once, reject it, and see stale/unauthorized applications blocked |
| **Developer 2 / B: Agents, backend, and integration** | Shared contracts, API, persistence, provider calls, all three role agents, bounded coordinator (current two-turn profile), trace, fallback integration | A run invokes independent ATC/Pilot/Ground agents, terminates within its budget, and exposes a persisted, truthful outcome through the API |
| **Developer 3 / C: Data, deterministic planning, and evaluations** | Updated CSV/workbook inspection, source extraction, scenarios, candidate enumeration, all domain checks, scoring, fallback selection, both baselines, evaluation runner/report | The same snapshot produces a reproducible checked catalog; measured results compare the system with B0/B1 and expose failures |

**Developer 2 is the integration owner.** Each developer writes and tests their own implementation. Developer 3 runs the combined evaluation suite; they do not inherit everyone else's testing.

The three developers are not assigned one aviation agent each. All three role agents belong to Developer 2 so their contracts and coordination work together. Developers 1 and 3 supply the human-control and deterministic-planning systems those agents require.

## 2. File ownership

One developer owns each file. Request a change from its owner when a shared interface needs adjustment; avoid concurrent edits to shared modules.

| Owner | Files and directories |
|---|---|
| Developer 1 | `app/static/`, `app/approvals.py`, `tests/test_approvals.py` |
| Developer 2 | `app/main.py`, `app/contracts.py`, `app/agents.py`, `app/coordinator.py`, `app/store.py`, `tests/test_coordinator.py`, `tests/test_api.py`, dependency/configuration files, setup instructions |
| Developer 3 | `app/data_adapter.py`, `app/scenarios.py`, `app/planning.py`, `app/constraints.py`, `scenarios/`, `policies/`, `evals/`, `tests/test_planning.py`, `tests/test_data.py` |

Generated traces and reports go under `artifacts/`; they are outputs, not manually edited shared source. Some paths are target files rather than existing modules; core application code now exists. Workbook ingestion, new-release rebinding and frozen evaluations remain pending.

### Important boundary between Developers 1 and 2

- **Developer 1 implements approval decisions:** authorization requirements, expected plan/revisions, expiry, revalidation, rejection, and the simulator resource update.
- **Developer 2 implements storage mechanics:** SQLite schema, transaction helper, event append/read, and idempotency storage.
- Developer 1 performs approval through Developer 2's transaction helper; Developer 2 mounts Developer 1's handlers in the API.
- **Freeze that helper's signature in the first 15 minutes.** Revalidation and application must execute inside the same transaction, using the current resource state. An unguarded update after a separate check is insufficient.

### Important boundary between Developers 2 and 3

- **Developer 3 selects the deterministic fallback candidate** from the full checked catalog.
- **Developer 2 decides when fallback is triggered**, calls that selector, persists the reason, and labels the result as fallback rather than consensus.
- **Developer 3 calculates all numbers and constraint results.** Developer 2 passes those results to agents and validates their cited IDs. Developer 1 renders trusted values from those results.

## 3. Developer 1 checklist — interface and human control

### Build

- [ ] A single dashboard with scenario selection, start/cancel, status, actual turn count/limit, profile and model-call count; map existing `round_count` to agent turns.
- [ ] Three agent cards showing independent offers, concessions, and final votes.
- [ ] Candidate comparison with landing, gate-in, gate, route, fuel margin, overtime, and other available impact values.
- [ ] Expandable constraint evidence with CSV release/hash, workbook sheet/cell or real citation, original/normalized units, applicability and historical/synthetic labels.
- [ ] Clear states for consensus, fallback, insufficient data, no feasible option, stale, rejected, and applied.
- [ ] Approve simulated plan, reject with reason, and revise-and-rerun actions.
- [ ] Backend approval logic that uses the persisted plan, validates the trusted reviewer session, checks versions/expiry, reruns constraints, and applies resources atomically.
- [ ] Download links for the trace and Developer 3's measured evaluation report.

### Test and demonstrate

- [ ] Nothing changes before approval; rejection and cancellation leave resources unchanged.
- [ ] A valid approval changes the simulated schedule exactly once.
- [ ] Missing reviewer authority, stale versions, expiry, duplicate approval, and competing resource use are handled correctly.
- [ ] No apply button is available for unknown/failed required checks or a no-plan result; the backend enforces the same rule.
- [ ] Present the success and failure flows in a rehearsed demonstration below 180 seconds.

**Inputs:** Developer 2's API/sample response and transaction helper; Developer 3's `check_plan` function and scenario facts.  
**Outputs:** Working browser flow, approval handler, approval tests, and demonstration.  
**First handoff:** Dashboard works with the agreed sample payload by minute 45. Full human-reviewed application works by minute 135.

**Scope limit:** Plain HTML/CSS/JavaScript is sufficient. Cut maps, animation, elaborate charts, account-management screens, and public hosting before reducing approval correctness.

## 4. Developer 2 checklist — agents, backend, and integration

### Build

- [ ] Publish strict shared schemas and one sample snapshot/run response in the first 15 minutes.
- [ ] Start one FastAPI service, serve the dashboard, and expose scenario/run/event/decision/cancel/export routes.
- [ ] Implement SQLite persistence, ordered events, resource revisions, and the transaction helper agreed with Developer 1.
- [ ] Smoke-test the available provider/model and record model/settings/prompt versions; disable automatic retries.
- [ ] Implement separate ATC, Pilot, and Ground wrappers with separate prompts, context, responses, and request identities.
- [ ] Round 1: run three independent calls concurrently; validate offers against known candidates and concession envelopes.
- [ ] Reconcile offers deterministically using Developer 3's checked costs and the PRD selection policy.
- [ ] Round 2: ask all three agents to accept/reject exactly the same plan hash; require three current accepts.
- [ ] Enforce one structured message per role per turn and the declared profile: current two turns/six requests within the organizer ceiling of three turns/nine requests, plus per-call timeouts and the overall deadline. Any third-turn extension must update/test contracts, storage, coordinator, prompts and UI together.
- [ ] Trigger Developer 3's fallback on dissent, timeout, or protocol failure; preserve the failure reason.
- [ ] Stop a run if its scenario/resources/rules change; reject late responses and preserve terminal outcomes.
- [ ] Persist source/version references, checks, messages, votes, counters, outcome, and review state; provide a readable export.

### Test and demonstrate

- [ ] Three separate real model requests are visible; three role labels around one request do not satisfy this deliverable.
- [ ] Wrong hashes, missing votes, malformed output, invented candidate IDs, and mismatched revisions cannot produce consensus.
- [ ] Timeout or dissent terminates within the declared profile: no seventh model request in the current profile and no tenth in a future three-turn profile; fallback makes zero model calls and adds no agent turn.
- [ ] Duplicate/late messages cannot add votes or overwrite the outcome.
- [ ] Model-supplied text cannot change hard rules, counters, reviewer identity, or application state.
- [ ] A genuine conflict/concession scenario ends in three matching real accepts, with its trace retained.

**Inputs:** Developer 3's typed catalog/checker/fallback functions; Developer 1's approval handlers.  
**Outputs:** Running API, provider adapter, three agents, coordinator, store, trace exports, and clean-start instructions.  
**First handoff:** Shared contracts/sample payload by minute 15; API skeleton by minute 45; first live recommendation by minute 90.

**Scope limit:** Use an explicit state machine, concurrent async calls, and polling. Cut orchestration frameworks, recursive tools, dynamic agent discovery, and streaming infrastructure if they slow integration.

## 5. Developer 3 checklist — planning, data, and evaluation

### Build

- [ ] Profile the updated organizer CSV independently: reported 358,774 rows, 2015, strict eligibility, actual schema/hash and exclusions. Retain the old full-file profile separately.
- [ ] Re-extract qualifying rows, verify natural identity and new ordinals/hash, and preserve original fields/eligibility. Do not reuse legacy D01 record 71 blindly.
- [ ] Inspect `challenge2_operational_constraints.xlsx`; record filename/hash, sheets/cells, airport/category keys, units and applicability. Cited real substitutions retain publisher/document/revision/section evidence.
- [ ] Map airport gates/runways, movements/hour/turnaround, wake separation, fuel and crew rules into versioned scenarios. Missing critical values stay unknown; do not claim workbook compliance for unmapped fixture constants.
- [ ] Preserve missingness, exclude unresolved airport identifiers, and keep historical outcomes outside agent decision inputs.
- [ ] Create versioned operational overlays from workbook-synthetic or cited limits plus separate scenario state: one focal flight, one runway, two gates, fixed background traffic and up to two routes per gate. Validate subset counts against airport totals and declare capacity/background-load assumptions; the filtered cohort is not a full traffic/rotation history.
- [ ] Enumerate the bounded landing/gate/route catalog: at most 124 raw combinations in the default scope.
- [ ] Implement all required domain checks: runway/spacing/capacity, taxi availability, fuel/arrival permission, gate compatibility/occupancy, crew/equipment, and required-data validity.
- [ ] Return `PASS/FAIL/UNKNOWN` with evidence IDs, units, and defined margins; failed/unknown candidates are not reviewable.
- [ ] Calculate impact values, domain costs, allowed concession envelopes, and the shared shortlist.
- [ ] Implement B0/ fallback: earliest feasible landing/gate-in under stable ordering.
- [ ] Implement B1: best joint score over the same full feasible catalog and constraints.
- [ ] Prepare six development scenarios and freeze the 12-case evaluation manifest before final prompt tuning.
- [ ] Build a runner that invokes the same application pipeline, compares applicable B0/B1 results, and retains every outcome.
- [ ] Produce JSON/CSV results plus a readable report with counts, latency, failures, assumptions, and limits.

### Test and demonstrate

- [ ] Independently hand-check workbook/cited separation, gate-interval, capacity, fuel and crew boundaries; have Developer 1 review arithmetic and units. Changes to mappings/thresholds create new versions and invalidate old review bindings.
- [ ] Missing fuel or rule applicability blocks a recommendation; no feasible plan produces a non-executable escalation.
- [ ] Changing reference-only historical outcomes leaves catalogs, scores, and fallback unchanged.
- [ ] Both baselines use the same snapshots, information boundaries, catalog, and hard checks as the full system.
- [ ] Report nominal consensus separately from injected-fault recovery; repetitions do not inflate independent scenario counts.
- [ ] Keep all failure/regression results and report the actual evaluated count if fewer than 12 cases finish.

**Inputs:** Developer 2's schemas and callable run/status interface; approval-test outcomes from Developer 1.  
**Outputs:** Pure planning/checking functions, source/overlay manifests, deterministic baselines, fixtures, and measured evaluation artifacts.  
**First handoff:** One usable checked catalog and baselines by minute 45; complete checks by minute 90; initial measured report by minute 180.

**Scope limit:** Use one reusable overlay template with small case variations. Cut full-dataset browsing, live data, trajectory optimization, extra airports, optional repeats, and optional ablations before cutting either baseline or failure evidence.

## 6. Freeze these interfaces first

Developer 2 publishes the schemas; each developer confirms their owned signatures. No contract changes after minute 15 without notifying both other developers.

| Owner | Interface | Required output |
|---|---|---|
| 3 | `load_scenario(id, revision)` | Typed immutable snapshot, source locator, synthetic facts, versions |
| 3 | `build_catalog(snapshot)` | Full checked catalog, feasible shortlist, impacts/costs, references |
| 3 | `check_plan(snapshot, plan)` | Required rule results; any `FAIL/UNKNOWN` blocks review/application |
| 3 | `select_fallback(snapshot, catalog)` | Checked candidate or typed no-plan/insufficient-data result |
| 3 | `select_baselines(snapshot, catalog)` | B0 and B1 with identical assumptions |
| 2 | `start_run(snapshot_revision)` | Immediate run ID; processing continues in the service |
| 2 | `get_run(run_id)` | State, outcome, plan, checks, role offers/votes, counters, review state |
| 2 | `get_events(run_id, after_seq)` | Ordered incremental events |
| 2 | Atomic review transaction helper | Current state locked for Developer 1's revalidation and application; one commit |
| 1 | `review(run_id, decision, expected_versions, idempotency_key)` | Applied/rejected/stale/unauthorized result and event |
| 3 | `evaluate_suite(manifest)` | Per-case system/baseline results and report |

Minimum shared run fields: `run_id`, `scenario_revision`, `resource_revision`, `ruleset_version`, `policy_version`, `processing_state`, `negotiation_outcome`, `approval_state`, `round_count`, `model_call_count`, `plan_hash`, `candidate`, `checks`, `offers`, `votes`, and `fallback_reason` where applicable.

Carry workbook/citation evidence in versioned facts and retain the actual protocol profile/limits in run/evaluation evidence. A workbook or citation must not be invented by the model. Use seconds, kilograms and explicit UTC simulation epochs consistently. Keep live, fixture, recorded-replay, and fallback modes visible in payloads and UI. Developers 1 and 2 use typed sample outputs while Developer 3 finishes planning; Developer 3 uses deterministic agent stubs while Developer 2 finishes provider wiring. Replace these at the live integration checkpoint.

## 7. Shared clock and handoffs

| Time | Developer 1 | Developer 2 | Developer 3 | Required checkpoint |
|---|---|---|---|---|
| **0–15 min** | Agree UI/review payload and transaction needs | Publish contracts, sample payload, API skeleton; verify model access | Inspect input availability, units/rules and workbook scope; rebind seed and create overlay | Everyone can build independently against the same contract |
| **15–45 min** | Dashboard with sample data; approval skeleton | Store/events, agent wrappers, run API | Catalog, core checks, B0/B1; freeze scenario manifest | First real deterministic catalog integrated |
| **45–90 min** | Live polling, agent cards, evidence | Real Round 1/reconciliation/Round 2/fallback | Complete checks and boundary fixtures | Live three-agent recommendation by **90 min** |
| **90–135 min** | Atomic approve/reject/stale handling | Protocol guards, exports, cancellation; wire review handler | Runner and safety fixtures | One real run can be reviewed and applied by **135 min** |
| **135–180 min** | Approval tests, errors, demo flow | Live run support and protocol fixes | Frozen suite and both baselines; initial report | Feature freeze and measured evidence by **180 min** |
| **180–210 min** | Final clarity and cross-review | Blocking fixes; labelled backup trace | Rerun affected cases; audit report | Release gates checked by **210 min** |
| **210–240 min** | Rehearse and lead presentation | Verify setup/deliverable; perform agreed submission | Verify evidence links and submission confirmation | Valid submission recorded; presentation below 180 seconds |

**At each checkpoint, integrate immediately.** A missing dependency becomes the owner's first task. Developer 2 tracks the combined build; the others continue independent work until pairing is necessary.

Cross-review once the vertical slice works: Developer 2 reviews Developer 1's approval guards; Developer 1 checks Developer 3's arithmetic/evidence; Developer 3 checks Developer 2's same-plan consensus and counter limits.

## 8. Definition of done for the whole team

- [ ] One real supplied row satisfies `WEATHER_DELAY > 15 OR LATE_AIRCRAFT_DELAY > 20` and its allowed parameters reach all three agents.
- [ ] Three independent agents send one structured message each per turn and commit to one exact plan within three turns/nine requests in a genuine conflict case; also satisfy the actual profile limit (current two turns/six requests).
- [ ] The updated historical source identity is hash-bound and workbook/cited operational mappings are inspected, unit/applicability checked and visible; legacy fixtures/results are labelled separately.
- [ ] Deterministic checks protect all approval-ready plans; unknown essential facts block them.
- [ ] Model failure produces deterministic fallback; an impossible or missing-data case produces a visible non-executable result.
- [ ] A human can approve/reject; stale, unauthorized, duplicate, and competing applications are handled correctly.
- [ ] The trace links the recommendation to inputs, policies, checks, messages, votes, and review decision.
- [ ] Measured B0/B1/system results and a visible failure are available; completed counts and limitations are stated accurately.
- [ ] A teammate can start the project using the setup instructions; the demo is rehearsed and the actual submission is confirmed.

If integration slips, remove visual polish, optional repeats/ablations, extra scenarios beyond the planned suite, and hosting work. Preserve the complete flow, independent agent calls, hard checks, fallback, human approval, both baselines, and honest failure evidence. Record incomplete coverage explicitly rather than inventing results.
