# Practical implementation idea

The organizer MVP is implemented: verified filtered data, workbook imports,
three turns/nine requests, deterministic fallback, B0/B1 comparisons and human
approval of runway/gate/crew/equipment bookings. See [the submission guide](submission.md)
for commands, measured results and simulation assumptions. The delivery priority
is the working demo; broader evaluation is optional.

Updated for the [31 October organizer clarification](organizer_updates.md). Start with a small deterministic scenario and make the full path observable: historical trigger, sourced operational limits, independent agents, exact-plan agreement, human review and a recorded simulator update.

## 1. Keep the numerical and authority boundaries explicit

Pure Python functions load/validate facts, enumerate alternatives, calculate impacts and check every hard rule. A deterministic coordinator chooses among mutually offered candidates and manages deadlines. Three independently invoked model agents express domain preferences and explain concessions. The API/store own mutations, versions, identity and the audit log.

A hard check can be `PASS`, `FAIL` or `UNKNOWN`. Only complete passing checks make a plan reviewable. An agent cannot invent a gate, waive separation, reduce reserves, alter duty limits or approve a resource write.

## 2. Use the correct two input sources

The organizer reports a new `flights.csv` with **358,774 records from 2015**, selected by `WEATHER_DELAY > 15 OR LATE_AIRCRAFT_DELAY > 20`. The historical row supplies identity and the disruption trigger. It does not supply gates, runways, fuel or crew, and the filtered file is not a complete airport movement schedule or aircraft rotation.

Use `challenge2_operational_constraints.xlsx` for the synthetic per-airport gates, runways, movements/hour and turnaround, plus wake separation, fuel and crew rules. Cited real figures are also accepted. Preserve exact evidence locations, units, applicability and source versions. Treat any additional current state, routes, blockages, equipment or connections as separate declared scenario inputs unless sourced.

The workbook and filtered CSV are imported with source bindings. Legacy D01 remains a separate regression fixture.

Do not confuse an airport's total resources with the one-runway/two-gate subset modeled by this prototype. Validate subset sizes and explain how background traffic and capacity windows are modeled.

## 3. Make turn accounting unambiguous

**One turn = one structured message from ATC, one from Pilot and one from Ground.** Calls may run concurrently. The organizer permits consensus within three turns, at most nine agent generation requests, then deterministic fallback. Fallback uses no LLM and consumes no agent turn. Log started calls and invalid/missing responses; a partial turn supplies no implicit vote.

The implemented MVP supports three turns/nine requests:

| Stage | Work | Agent messages |
|---|---|---:|
| Preparation | Freeze facts and enumerate/check a finite catalog | 0 |
| Turn 1 | Each role offers preferred and acceptable/concession candidate IDs with evidence | 3 |
| Reconciliation | Deterministically select a common candidate using checked costs | 0 |
| Turn 2 | Each role explicitly accepts/rejects the identical complete plan hash | 3 |
| Turn 3, if needed | After valid dissent, select an untried common plan and collect three fresh votes | 3 |
| Completion | Recheck consensus or use deterministic fallback/escalation | 0 |
| Human review | Approve/reject the exact fresh plan, with reason | 0 |

The third turn is implemented across contracts, storage, prompts, coordinator and display. It requires an untried common candidate and enough time for all three fresh votes on its immutable plan hash. Otherwise fallback decides. There is no fourth-turn confirmation. See PRD Section 9.

## 4. A simple conceptual control flow

This pseudocode shows the initial two turns. A valid dissent additionally permits one revised proposal and fresh votes from all roles; otherwise fallback decides:

```python
snapshot = freeze_and_validate(source_row, operational_evidence, scenario_state)
catalog = enumerate_and_check(snapshot)
if catalog.required_inputs_missing:
    result = insufficient_data(catalog.blockers)
else:
    offers = independent_role_messages(turn=1, catalog=catalog.shortlist)
    proposal = reconcile_valid_offers(offers, catalog)
    votes = independent_role_messages(turn=2, proposal=proposal) if proposal else []
    if three_current_accepts_of_same_plan(votes, proposal) and recheck(snapshot, proposal):
        result = consensus(proposal, offers, votes)
    else:
        result = deterministic_fallback(snapshot, catalog)  # full catalog, zero model calls
persist_result_and_trace(result)
# A later authenticated human decision rechecks current resources in one transaction.
```

Production code also enforces request/deadline budgets, cancellation, strict schemas, input freshness, duplicate/late-message guards and explicit provider failures. Fallback may return no feasible option or insufficient data; there is no unchecked emergency card or assumption that some alternative must be allowed.

## 5. Build and verify in small steps

1. Hand-check a small workbook-backed or explicitly cited scenario, including an impossible and a missing-data variant.
2. Validate row identity and operational evidence; derive an immutable versioned JSON snapshot.
3. Enumerate the bounded catalog and independently verify fuel, separation, gate, crew and capacity boundaries.
4. Exercise coordinator, fallback and human-review guards with labelled agent fixtures.
5. Replace fixtures with three actual independent provider calls per turn; preserve every failed attempt.
6. Render the persisted events, checks, source evidence, actual counters and uncertainty on one page.
7. Run the frozen suite and B0/B1 comparisons through the same pipeline; report actual outcomes and limitations.

Keep a worked D01 arithmetic example as a legacy regression fixture until its constants and locator are verified against the new input sources. Its old numbers cannot establish workbook compliance.

## 6. Human control and audit

Negotiation is autonomous until a terminal recommendation. The reviewer then sees historical and operational provenance, exact checks and margins, domain trade-offs, same-hash votes, expiry and fallback status. Both approval and rejection require a reason. Fallback needs an explicit acknowledgement that consensus failed.

Nothing changes before approval. The shared SQLite transaction revalidates current resources and commits the reservation, revision and review event exactly once. Stale or unknown checks, unauthorized requests, duplicate applications and conflicting resource use are blocked. Persisted application reserves runway, gate, handling crew and required equipment together in the simulator.

The hash-chained log supports inspection but is not tamper-proof against the database owner. Store concise explanations and evidence, never hidden model reasoning or secrets.

## 7. Divide work among exactly three developers

| Owner | Responsibility | Evidence of completion |
|---|---|---|
| Developer 1 / A | Dashboard, source/evidence display, review logic and simulator application | A reviewer can inspect, approve once, reject and see stale/unauthorized cases blocked |
| Developer 2 / B | All three agents, contracts, provider adapter, coordinator, API/store and traces | Separate requests/messages per role and turn, truthful terminal outcomes and counters |
| Developer 3 / C | Updated CSV/workbook ingestion, scenarios, rules, catalog/fallback, B0/B1 and evaluations | Reproducible sourced checks and paired measured results with failures retained |

See [the detailed work plan](developer_work_plan.md). One service, plain Python functions and the existing HTML/JavaScript dashboard are sufficient. Avoid adding an agent framework or separate service to handle this clarification.

## 8. Explain and evaluate the result honestly

A concise pitch: “We start with a qualifying historical flight and explicit operational evidence. Deterministic rules check the alternatives. Three independent agents exchange structured preferences and commit to the same plan within the declared turn budget, or deterministic fallback exposes a checked alternative or a blocker. A human reviews the evidence before anything changes in the simulator.”

Show actual turn/message/call counts, a conflict/concession trace, one failure, a reasoned human decision and measured B0/B1 comparisons. Report the workbook's synthetic status and all remaining implementation gaps. Source row count, a successful demo and unit-test count do not establish operational benefit or a general convergence rate.
