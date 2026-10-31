# Developer B implementation and integration handoff

This branch implements the backend, contracts, three role agents, coordination,
persistence, trace export, and the storage boundary for human review. It now
integrates Developer C's deterministic checker and fallback from
[PR #1](https://github.com/kaseyho/PLAB.ai/pull/1). Dashboard/simulator integration
and the frozen evaluation suite remain with their owners in the
[work plan](developer_work_plan.md).

## Specification decisions

The [technical PRD](technical_prd.md) and challenge requirements govern this
implementation. The high-level guide contributes the simple functional design,
deterministic safety boundary, and hash-chained log. Its illustrative single-LLM,
third-turn, no-database examples conflict with the explicit PRD. This service
therefore uses three independent calls per round, two rounds, a checked
deterministic fallback, SQLite, and human approval. There is one service process;
no agent framework, message broker, recursive tool loop, or separate microservice.

## Module ownership

| Module | Responsibility |
|---|---|
| `app/contracts.py` | Immutable Pydantic contracts, enums, canonical hashes, mandatory rule coverage |
| `app/agents.py` | Scoped prompts, structured provider requests, offer/vote validation |
| `app/coordinator.py` | Two concurrent role rounds, deterministic reconciliation, deadlines and fallback |
| `app/store.py` | SQLite transactions, resource revisions, request accounting, hash-chained events |
| `app/main.py` | API, trusted local session, composition and teammate integration |
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
  fields, and computes the actual file hash. Missing source files are reported
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

D01 produces 93 checked candidates, 14 feasible candidates, and a six-candidate
shortlist. The procedural fallback is `L600-G2-route`; the worked compromise is
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

## Developer A: connect review and the dashboard

The API mounts `app/static/` and serves its `index.html`. Use the supplied
`examples/sample_snapshot.json` and `examples/sample_run.json` for initial UI
work, or run `uv run python -m examples.backend_demo --serve`. Sample plans and
statuses are fixtures and must be labelled as such in the UI.

Implement the following function in `app/approvals.py`:

```python
from app.contracts import ResourceState, ReviewEffect, ReviewRequest, RunRecord

def review_plan(
    run: RunRecord, resource: ResourceState, request: ReviewRequest
) -> ReviewEffect:
    # Recheck the shared snapshot/plan using app.planning.check_plan.
    # Compute resource reservations/application according to A's simulator rules.
    # Return all check results and the new resource data, or raise ServiceError.
    ...
```

`Store.review(run_id, request, key, reviewer_id, hook)` calls this function
**inside `BEGIN IMMEDIATE`**, with the current persisted resource revision locked.
The hook must be synchronous, pure, fast, and free of network/storage side effects.
It returns `ReviewEffect(checks=..., resource_data=...)`; it must not commit or
increment resource revisions itself. The store verifies complete passing checks,
rechecks expiry, commits the resource update and audit record together, and
increments the revision exactly once. Exceptions roll back the transaction.
The fixture hook is an example of the signature, not the simulator implementation.

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
new run (`parent_run_id`). For integrated overlays, update the derived
`snapshot.versions` fact to match the revised envelope; stale version evidence is
rejected before planning. Ordinary revision requests cannot edit policy versions,
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
- Limits: two rounds, six started requests, 12 seconds per request, 45 seconds per
  run, five seconds reserved for fallback, 500 output tokens, zero retries.
- Agent failures, malformed JSON, invented IDs, wrong hashes, or dissent enter
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
The default service now integrates PR #1's real deterministic checker and D01
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

These smokes are development checks, not frozen evaluation results. A's
simulator/dashboard integration and C's remaining scenarios, B1 comparison,
evaluation runner and measured report are still required.
