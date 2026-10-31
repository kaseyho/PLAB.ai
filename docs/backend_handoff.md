# Developer B implementation and integration handoff

The organizer MVP is implemented: verified filtered data, workbook imports,
three turns/nine requests, deterministic fallback, B0/B1 comparisons and human
approval of runway/gate/crew/equipment bookings. See [the submission guide](submission.md)
for commands, measured results and simulation assumptions. The delivery priority
is the working demo; broader evaluation is optional.

This branch implements the backend, contracts, three role agents, coordination,
persistence, trace export, and the storage boundary for human review. It now
integrates Developer C's deterministic checker and fallback from
[PR #1](https://github.com/kaseyho/PLAB.ai/pull/1) and Developer A's duty desk and
simulated approval gate from [PR #2](https://github.com/kaseyho/PLAB.ai/pull/2), with
booking validation and dashboard evidence from [PR #4](https://github.com/kaseyho/PLAB.ai/pull/4).
The frozen evaluation suite remains with its owner in the
[work plan](developer_work_plan.md).

## Organizer implementation

The submission app loads scenarios/organizer/ and supports three turns/nine
requests. app/operational_data.py imports the workbook; app/prepare.py binds the
358,774-row release to identities and source hashes. Legacy D01 and old smokes
remain historical evidence. app/demo.py launches an isolated live or offline
demo. app/evaluate.py provides optional B0/B1 evaluation. Approval now reserves
runway, gate, crew and equipment in the same transaction.

## Specification decisions

The [technical PRD](technical_prd.md) and challenge requirements govern this
implementation. The high-level guide uses the same functional design,
deterministic safety boundary and hash-chained log. This service uses three independent calls per
turn, a three-turn/nine-request limit, a
checked deterministic fallback, SQLite and human approval. Turn 3 requires fresh unanimous votes on an untried common proposal.
There is one service process;
no agent framework, message broker, recursive tool loop, or separate microservice.

## Module ownership

| Module | Responsibility |
|---|---|
| `app/contracts.py` | Immutable Pydantic contracts, enums, canonical hashes, mandatory rule coverage |
| `app/agents.py` | Scoped prompts, structured provider requests, offer/vote validation |
| `app/coordinator.py` | Two concurrent role rounds, deterministic reconciliation, deadlines and fallback |
| `app/store.py` | SQLite transactions, resource revisions, request accounting, hash-chained events |
| `app/main.py` | API, trusted local session, composition and teammate integration |
| `app/approvals.py` | Recheck current reservations and compute the simulated resource update |
| `app/static/` | Duty desk using the shared API, plus explicit labelled sample views |
| `app/planning.py` | Boundary from C's scenario/checker models to shared run contracts, scoring and shortlist |
| `app/scenarios.py`, `app/constraints.py`, `app/fallback.py` | C's operational definitions, hard rules and full-catalog procedural selection |
| `examples/backend_fixture.py` | Explicit protocol fixtures; never automatically selected by the main service |
| `examples/backend_demo.py` | Runnable fixture API and offline/live smoke command |

## Developer C: integrated planning and remaining evaluations

These synchronous, pure, bounded integration functions are implemented. Their
public types come from `app.contracts`:

```python
# app/planning.py
def build_catalog(snapshot: ScenarioSnapshot) -> CatalogResult: ...
def select_fallback(snapshot: ScenarioSnapshot, catalog: CatalogResult) -> FallbackResult: ...
def check_plan(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> tuple[ConstraintResult, ...]: ...
```

The service discovers these exact functions at startup. For tests or a suite,
inject `PlanningHooks(build_catalog, check_plan, select_fallback)` into
`create_app` or `Coordinator`. `app.constraints.check_plan` retains C's native
scenario/plan signature; call `app.planning.check_plan` for a shared backend
snapshot. The adapter reuses C's rule implementation and rederives shared impacts
and costs before accepting a plan.

- Save C's overlay/source definitions or prepared shared `ScenarioSnapshot` JSON
  files directly under `scenarios/`. The service imports them at startup without
  overwriting existing revisions. For overlay definitions, the loader reads the
  exact CSV record from `PLAB_DATA_DIR` (default `data/`), retains all original
  fields, and computes the actual file hash. This locator is file-specific:
  updated-release adoption needs identity/eligibility verification and a new
  ordinal/hash. Missing source files are reported
  in `/health`; no placeholder record is substituted.
- Facts carry domain scope, provenance, units, source/version references, and
  optional expiry. The adapter freezes each operational overlay section as a
  fact and maps rule evidence paths to those sections. Derived inputs belong in
  `Fact.input_ids`. Missing critical inputs terminate before any model call;
  unknown connections are not treated as zero passengers. Historical outcomes
  remain reference data and do not affect operational scoring.
- The complete catalog has at most 124 candidates and a shortlist of at most six.
  Include all failed/unknown check results and search-limit/blocker information.
  Only fully passing candidates belong in the shortlist.
- Return one aggregate result per rule per candidate, including the eleven
  mandatory PRD rules. Check IDs are unique within a candidate; evidence IDs
  reference snapshot facts. Required rules cannot be removed by the client.
  Rule-domain and candidate/version mismatches are rejected.
- Use seconds relative to the explicit simulation epoch, kilograms, minutes for
  overtime/lateness, and decimal domain costs. The checker owns hard-rule
  arithmetic and the adapter implements PRD 8.5/8.6 impacts and `balanced-v1`
  costs. The coordinator orders costs and enforces concession envelopes.
- The fallback selector receives the **full catalog**, applies C's B0 ordering,
  and returns a candidate ID or a typed non-executable outcome. The coordinator
  calls `check_plan` again before making a fallback reviewable.
- Planning calls have a two-second default timeout. They run off the event loop
  and must not access storage, make network calls, or perform unbounded work.
  Python cannot forcibly stop a timed-out worker thread; purity prevents late
  computation from committing state.

The current legacy-input, illustrative D01 fixture produces 93 checked candidates,
14 feasible candidates, and a six-candidate shortlist.
The procedural fallback is `L600-G2-route`; the worked compromise is
`L960-G1-long`, with costs `(ATC=0.2, PILOT=0.3, GROUND=0)`. The fallback enumerates
and checks the full catalog again before selection, followed by a shared-plan
recheck in the coordinator. Failed candidates retain their diagnostics, including
negative projected fuel where applicable, and cannot become reviewable.

For evaluations, call the same `Store.create_run` and `Coordinator.run` pipeline
as the API, use a new run/idempotency key per attempt, and retain `Store.export`.
`run.elapsed_ms` measures monotonic processing latency; it is unknown after a
process crash rather than synthesized from wall-clock times. API runs include
request admission/storage time. If invoking directly, pass an optional
`accepted_at=time.monotonic()` captured before `create_run` to `Coordinator.run`.
Count `model_call_count` as started live requests, including failed attempts;
fixture invocations are counted separately. A cancelled/timed-out request may
later acquire a `late_result` containing returned token usage. Never count a
single request twice or treat absent usage as zero billed usage.

Write the measured report to `artifacts/evals/latest.json`. The report endpoint
only reads that file; it never silently launches a billable evaluation. Baselines,
scenario freezing, denominators, and report provenance remain C's responsibilities.

## Developer A: integrated review and dashboard

The API mounts `app/static/` and serves its `index.html`. The dashboard starts with
the actual available scenarios. Its local token field supplies authorization and
its mutations use idempotency headers. Polling uses the shared event sequence;
costs and impacts come from the checked catalog. Model calls and fixture agent
invocations are distinct. Sample views require an explicit `?fixture=<name>`.

The implemented hook in `app/approvals.py` uses the shared contracts:

```python
from app.contracts import ResourceState, ReviewEffect, ReviewRequest, RunRecord

def review_plan(
    run: RunRecord, resource: ResourceState, request: ReviewRequest
) -> ReviewEffect:
    # Merge current committed reservations and recheck with app.planning.check_plan.
    # Return checked runway/gate reservations; the Store commits them atomically.
    ...
```

`Store.review(run_id, request, key, reviewer_id, hook)` calls this function
**inside `BEGIN IMMEDIATE`**, with the current persisted resource revision locked.
The hook must be synchronous, pure, fast, and free of network/storage side effects.
It returns `ReviewEffect(checks=..., resource_data=...)`; it must not commit or
increment resource revisions itself. The store verifies complete passing checks,
rechecks expiry, commits the resource update and audit record together, and
increments the revision exactly once. Exceptions roll back the transaction.
The fixture hook remains isolated in `examples/backend_fixture.py`; the default
service uses the real simulator hook. Applied reservations are stored separately
from editable facts and included in refreshed snapshots, preventing an edit from
accidentally deleting a committed runway movement or gate booking.

All mutations require the configured operator token. The reviewer identity comes
from server configuration, never from a client or model. Requests from a different
browser origin are rejected. A valid token is also required when the Origin header
is absent, allowing local CLI clients. Do not expose the token in public JavaScript,
logs, URLs, trace exports, or committed files. Render all model text as text.

The backend mechanically checks the persisted plan hash, exact revisions,
policy/ruleset versions, TTL, pending state, and fallback acknowledgment before
calling A's hook. The hook revalidates the actual simulator state and computes
application changes. Rejecting a plan does not call the hook or change resources.

### API payloads

`POST /api/runs` uses an `Idempotency-Key` header and this body:

```json
{"scenario_id":"backend-fixture","scenario_revision":1,"parent_run_id":null}
```

It returns 202 immediately. Poll `GET /api/runs/{run_id}`. Run responses contain
top-level hashes, scenario/resource/rule/policy versions, counters, catalog,
offers, votes, candidate, checks, outcome, approval state, and the frozen snapshot.
Read `GET /api/runs/{run_id}/events?after_seq=N` incrementally. The same start key
and body return the original run and do not launch more requests.

`POST /api/runs/{run_id}/decision` also requires an idempotency key:

```json
{
  "decision": "APPROVE",
  "plan_hash": "copy the exact persisted hash",
  "scenario_revision": 1,
  "resource_revision": 1,
  "ruleset_version": "toy-rules-v1",
  "policy_version": "balanced-v1",
  "expires_at": "copy the exact persisted timestamp",
  "reason": "Reviewed the evidence",
  "acknowledge_fallback": false
}
```

Use `REJECT` with a reason to reject. Fallback approval requires
`acknowledge_fallback=true`. Replay with the same key/body returns the original
decision; changed bodies conflict. A different key cannot apply the same run again.
`POST /api/runs/{run_id}/cancel` is idempotent for cancellation and blocks later
application. It cannot undo an already applied plan.

Creating a revised snapshot requires `revision=previous+1` and
`resource_revision=current+1`; it invalidates active/pending runs on that resource.
Fetch `/api/resources/{resource_id}` for the current resource revision after an
application. Derive revised facts from that current resource state and use a linked
new run (`parent_run_id`). For integrated overlays, the API includes committed
reservations and updates derived `snapshot.versions` evidence during revision.
Direct planner callers must still provide consistent version evidence.
Ordinary revision requests cannot edit policy versions,
concession budgets, required rules/facts, or policy facts. Existing plans and source
revisions stay intact.

Errors return `{"error":{"code":"..."}}`: 422 for schema errors, 403 for session
or origin failure, 409 for state/version/idempotency conflicts, and 503 for missing
teammate integration or unavailable storage. GET routes do not require a token in
this localhost prototype. Run outcomes and approval states are separate: consensus
or fallback does not mean applied.

## Coordination and audit guarantees

- Round 1: three independent requests with separate role prompts/scoped evidence.
- Reconcile the common initial set, then permitted concessions, using the PRD's
  minimax cost tuple and stable tie breakers. Refusal remains possible.
- Round 2: three independent votes over the same complete plan hash, with all
  peer offers visible. Three current accepts and final passing checks are required.
- Round 3, when needed: valid dissent selects an untried common candidate within
  the remaining budget and collects three fresh votes on the revised plan hash.
- Limits: three turns, nine started requests, 12 seconds per request, 45 seconds per
  run, five seconds reserved for fallback, 500 output tokens, zero retries. These
  are the actual profile limits; the organizer permits up to three turns/nine
  requests. Each completed turn has one structured message from each role.
- Agent failures, malformed JSON, invented IDs, wrong hashes, or unresolved dissent enter
  deterministic fallback. Stale inputs interrupt instead of falling back from an
  obsolete catalog. Non-executable outcomes have no reviewable candidate.
- No resource write occurs during negotiation. Cancellation and terminal state
  guard against late replies. A restart marks active runs interrupted without
  reissuing requests. An old run's counters and evidence remain visible.
- Exports retain catalog checks, failed rechecks, role envelopes, provider
  requests/outputs/usage, versions, and review events. Prompt hashes identify exact
  request context. Original historical outcomes remain in the source record but
  the role prompt builder uses an explicit allowed-field list.
- Audit hashes detect changes against the retained chain; the database owner can
  rewrite the entire chain. This is not a tamper-proof ledger. No hidden model
  reasoning, credentials, or exception messages are intentionally stored.

## Verification and remaining team gates

The results below predate inspection/integration of the new workbook and filtered
release. They validate the legacy-input, two-turn implementation only; they cannot
be relabelled as workbook-backed benchmarks. Re-run affected source/rule checks and
paired comparisons after migration, preserving these original outcomes.

Automated tests cover fixture conflict/concession, matching final votes, refusal,
invalid output/references, crossed versions, duplicate requests/messages, missing
votes, timeout, late replies, restart, atomic competing approvals, stale/expired
review, and API access guards. SDK transport tests exercise six separately formed
requests, strict schemas, token settings, returned usage, and disabled retries.
Ruff and strict mypy check the implementation.

The supplied fixture's historical fields were compared with record 71 of the
local `data/flights.csv`; they match. Its operational checks remain explicitly
synthetic test certificates. Offline protocol success does not satisfy the live
model or deterministic aviation-evaluation gates.

Live conflict/concession verification succeeded on 2026-10-31 with SoCLaaS
`qwen3.8:27b`, Chat Completions, `reasoning_effort=none`, and prompt version
`roles-v5`. Run `55d875d9-dfc1-4542-bd62-3575071bd85b` made six separately identified
provider calls in two rounds and terminated in 8,595 ms. ATC and Pilot initially
preferred C1 and offered C2 as a concession; Ground preferred C3 and initially
accepted both C2 and C3. All three then returned `ACCEPT` for the same C2 plan hash.
The trace chain validates. Approval was `PENDING`; no resources were applied.

The full trace remains local under
`artifacts/backend-smoke/55d875d9-dfc1-4542-bd62-3575071bd85b.json`. It records 24,235
input tokens and 1,185 output tokens as reported by the gateway across the six
requests. The local `.env` and example configuration select the verified model
and endpoint; credentials remain ignored. The endpoint is fixed before a run,
not changed as an automatic retry.

Retain the earlier attempts: authentication failure, malformed/nonconforming
responses, offer-policy/ranking violations, valid refusals with no mutual offer,
and an output-token limit reached before a reasoning model returned its answer.
All terminated as explicit fallback, without weakening validators or exceeding
the request budget. See `artifacts/backend-smoke/verification.json` for the full
attempt inventory. These were development iterations over one synthetic fixture,
not independent scenarios or a frozen evaluation suite: ten live attempts in total,
one consensus, nine fallbacks, and 33 started requests across all attempts. The
first attempt was the earlier authentication failure. One successful attempt
does not establish a convergence rate. Rationale prose can still misstate costs
or relationships; render trusted metrics from deterministic results and retain
human review rather than treating JSON validation as semantic verification.

The historical provider verification above used fixture planning certificates.
The original development service integrated PR #1's deterministic checker and legacy D01
overlay; the explicit `examples.backend_demo` commands remain fixture demos.

An integrated API smoke run on 2026-10-31 used the supplied CSV and live SoCLaaS
provider: `991165fd-451a-4346-b625-a574342c1d92` reached `FEASIBLE_CONSENSUS` for
`L960-G1-long` in 15,116 ms, with six calls in two rounds. All eleven required
checks passed, all three roles accepted the same plan hash, and the exported audit
chain validated. Approval remained `PENDING`; no resources were applied. The trace
is local at `artifacts/integration/991165fd-451a-4346-b625-a574342c1d92.json`.

PR #1 then added stricter separation, sliding-capacity, and search-window checks
in `be04ca5`; this branch includes them. A second smoke with the final integration,
`00c38d8f-066c-4a5e-9739-00f9f5c609e8`, rejected a role's out-of-policy initial
offer (`INITIAL_POLICY_VIOLATION`) and produced checked fallback `L600-G2-route`.
It made three calls in one round and finished in 10,880 ms, still pending review
with resource revision unchanged. Its full trace remains beside the first one.
Both persisted plans pass all eleven rules under the final code; the recheck
results are in local `artifacts/integration/verification.json`. Across these two
development attempts there was one consensus, one fallback, and nine live calls;
no failed attempt was retried or omitted.

The combined suite passes 141 offline tests, Ruff lint/format checks, and strict
mypy. The tests include both teammates' fixtures, the upstream safety regressions,
actual source loading, complete catalog/shortlist/fallback behavior, plan/evidence
tampering, current version evidence, and the PRD overtime boundaries.

These smokes are development checks, not frozen evaluation results. C's remaining
scenarios, B1 comparison, evaluation runner and measured report are still required.

PR #2 integration adds coverage through the real SQLite store instead of the
contributor's temporary in-memory protocol. All 167 offline tests pass, including
authorization, binding/expiry, explicit fallback acknowledgement, competing
approvals, a failed resource write, revalidation against changed reservations,
and refresh preserving committed bookings. Ruff, strict mypy and JavaScript
syntax checks pass. Browser verification covered start, approval, refresh/rerun,
cancellation and all six sample states, with no console errors. Browser agents
were explicit fixtures and made zero live model calls.

Regenerate sample views using
`uv run python -m app.static.fixtures.build_fixtures`. They are generated through
the real coordinator/checker/review store, contain the actual source row, and
retain truthful zero-live-call counters. They include only a catalog/event view
projection; the API trace export remains the complete audit record.

PR #4 integration brings the combined suite to 176 passing offline tests. The
additional cases cover malformed reservations, duplicate booking detection,
snapshot purity, exact runway/separation/gate conflicts, and edits that remove a
committed gate. Invalid overlay edits return a validation response without
changing stored state. New reservations retain the candidate ID and plan hash;
the existing `reservations` storage format remains compatible with earlier runs.

The dashboard now requires a reason for approval as well as rejection, displays
domain costs and whether votes match the displayed plan hash, and includes a
seventh sample for rejection. Browser checks verified the wrong-token error,
reason-required controls, approval, refresh/rerun, rejection, and all seven sample
states. No browser console errors occurred. These checks used fixture agents and
the real planner/review store, with zero live model calls. Ruff lint/format,
strict mypy and JavaScript syntax checks also pass.

Simulated application persists runway and gate reservations only. Crew and
equipment availability are checked from scenario facts; persistent bookings for
those resources and the frozen evaluation suite remain outside these merged PRs.
