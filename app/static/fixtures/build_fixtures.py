"""Regenerate the dashboard's labelled sample payloads.

    python app/static/fixtures/build_fixtures.py

Plans, checks, fallback outcomes and approval results come from the real code on the D01
scenario and the real 2015 row (record 71). Agent offers and votes are illustrative and the
payload says so (`mode: "fixture"`); they are not a live model run. The payload shape is
Developer 1's assumption until Developer 2 publishes contracts.py.
"""

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.approvals import ReviewerSession, new_recommendation, review  # noqa: E402
from app.constraints import check_plan, derive_plan  # noqa: E402
from app.fallback import evaluate_catalog, select_fallback  # noqa: E402
from app.scenarios import snapshot_from_dict  # noqa: E402

OUT = Path(__file__).resolve().parent
RAW = json.loads((ROOT / "scenarios" / "D01.json").read_text())
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

SOURCE = {
    "label": "historical_observed, retrospective",
    "dataset_file": "flights.csv",
    "record_ordinal": 71,
    "natural_identity": "2015-01-01 AA1057 N3ASAA DFW-MIA sched 0515",
    "fields": {"YEAR": "2015", "MONTH": "1", "DAY": "1", "AIRLINE": "AA", "FLIGHT_NUMBER": "1057",
               "TAIL_NUMBER": "N3ASAA", "ORIGIN_AIRPORT": "DFW", "DESTINATION_AIRPORT": "MIA",
               "SCHEDULED_DEPARTURE": "0515", "SCHEDULED_ARRIVAL": "0856",
               "WEATHER_DELAY": "102", "LATE_AIRCRAFT_DELAY": "0", "CANCELLED": "0", "DIVERTED": "0"},
    "predicate": "(WEATHER_DELAY > 15) OR (LATE_AIRCRAFT_DELAY > 20)",
    "qualifies_by": "WEATHER_DELAY 102 > 15",
}


def snapshot(mutate=None):
    raw = copy.deepcopy(RAW)
    if mutate:
        mutate(raw)
    return snapshot_from_dict(raw)


def impacts(snap, plan, checks):
    """Raw values for the comparison table; scoring/normalisation belongs to the planning owner."""
    fuel = next(c for c in checks if c.rule_id == "PILOT-FUEL-01")
    crew = snap.overlay.crew(plan.crew_id)
    overtime_s = max(0, plan.service_end_s - max(plan.service_start_s, crew.regular_shift_end_s))
    return {
        "candidate_id": plan.candidate_id,
        "landing_time_s": plan.landing_time_s,
        "gate_in_time_s": plan.gate_in_time_s,
        "service_end_s": plan.service_end_s,
        "gate_id": plan.gate_id,
        "taxi_route_id": plan.taxi_route_id,
        "added_holding_min": (plan.landing_time_s - snap.overlay.search.earliest_landing_s) / 60,
        "fuel_at_gate_kg": fuel.actual,
        "fuel_margin_kg": fuel.margin,
        "crew_overtime_min": overtime_s / 60,
        "feasible": all(c.status == "PASS" for c in checks),
    }


def base_payload(snap, run_id):
    return {
        "mode": "fixture",
        "mode_note": "Sample payload. Plans and checks are computed by the real checker; agent offers and votes are illustrative, not a live model run.",
        "run_id": run_id,
        "parent_run_id": None,
        "scenario": {"scenario_id": snap.scenario_id, "description": snap.description,
                     "simulation_epoch_utc": snap.simulation_epoch_utc, "provenance": snap.provenance},
        "source": SOURCE,
        "scenario_revision": snap.scenario_revision,
        "resource_revision": snap.resource_revision,
        "ruleset_version": snap.ruleset_version,
        "policy_version": snap.policy_version,
        "processing_state": "TERMINAL",
        "round_count": 0,
        "model_call_count": 0,
        "plan_hash": None,
        "candidate": None,
        "checks": [],
        "candidates": [],
        "offers": {},
        "votes": {},
        "fallback_reason": None,
        "blockers": {},
        "missing_facts": [],
        "search_bounds": None,
        "baseline_b0": None,
        "expires_at": (NOW + timedelta(seconds=120)).isoformat(),
        "evaluation": None,
        "events": [],
    }


def with_plan(payload, snap, plan):
    checks = check_plan(snap, plan)
    payload.update(plan_hash=plan.plan_hash, candidate={**plan.model_dump(), **impacts(snap, plan, checks)},
                   checks=[c.model_dump() for c in checks])
    return payload


def shortlist(snap, ids):
    catalog = {c.plan.candidate_id: c for c in evaluate_catalog(snap)}
    return [impacts(snap, catalog[i].plan, catalog[i].checks) for i in ids]


def events(*rows):
    return [{"seq": i + 1, "type": t, "actor": a, "summary": s} for i, (t, a, s) in enumerate(rows)]


def consensus():
    snap = snapshot()
    plan = derive_plan(snap, 960, "G1-long")
    payload = with_plan(base_payload(snap, "run-fixture-consensus"), snap, plan)
    b0 = select_fallback(snap, "baseline").plan
    payload.update(
        negotiation_outcome="FEASIBLE_CONSENSUS", approval_state="PENDING", round_count=2, model_call_count=6,
        candidates=shortlist(snap, ["L600-G2-route", "L960-G1-long", "L1020-G1-long", "L1200-G1-short"]),
        baseline_b0=impacts(snap, b0, check_plan(snap, b0)),
        offers={
            "ATC": {"preferred_candidate_id": "L600-G2-route", "acceptable_candidate_ids": ["L600-G2-route"],
                    "concession_candidate_ids": ["L960-G1-long", "L1020-G1-long"],
                    "rationale": "Earliest slot keeps the arrival flow moving; a 6-minute delay behind BG2 is within my concession range."},
            "PILOT": {"preferred_candidate_id": "L600-G2-route", "acceptable_candidate_ids": ["L600-G2-route"],
                      "concession_candidate_ids": ["L960-G1-long"],
                      "rationale": "Least fuel used at 600 s. 960 s still leaves a positive checked reserve margin."},
            "GROUND": {"preferred_candidate_id": "L960-G1-long", "acceptable_candidate_ids": ["L960-G1-long", "L1200-G1-short"],
                       "concession_candidate_ids": [],
                       "rationale": "G2's crew shift ends at 1080 s, so 600 s means 20 minutes of overtime. G1 at 1500 s avoids it."},
        },
        votes={role: {"decision": "ACCEPT", "plan_hash": plan.plan_hash, "rationale": text} for role, text in {
            "ATC": "Spacing behind BG2 is exactly the 180 s minimum; accepted.",
            "PILOT": "Reserve margin stays positive (see PILOT-FUEL-01); I accept the 6-minute hold.",
            "GROUND": "G1 is free on arrival and the task finishes inside the regular shift.",
        }.items()},
        events=events(
            ("RUN_CREATED", "system", "Run created for D01 revision 1"),
            ("CANDIDATES_EVALUATED", "system", "93 candidates checked, 14 feasible"),
            ("AGENT_RESPONSE_RECEIVED", "ATC", "Round 1 offer"),
            ("AGENT_RESPONSE_RECEIVED", "PILOT", "Round 1 offer"),
            ("AGENT_RESPONSE_RECEIVED", "GROUND", "Round 1 offer"),
            ("PROPOSAL_SELECTED", "coordinator", "No common first choice; L960-G1-long is in every concession set"),
            ("VOTE_RECORDED", "ATC", "ACCEPT"), ("VOTE_RECORDED", "PILOT", "ACCEPT"), ("VOTE_RECORDED", "GROUND", "ACCEPT"),
            ("CONSENSUS_REACHED", "coordinator", "Three matching accepts on the same plan hash"),
            ("REVIEW_REQUESTED", "system", "Awaiting duty manager"),
        ),
    )
    return payload


def fallback():
    snap = snapshot()
    outcome = select_fallback(snap, "PILOT rejected the proposal in round 2")
    payload = with_plan(base_payload(snap, "run-fixture-fallback"), snap, outcome.plan)
    payload.update(
        negotiation_outcome="FEASIBLE_FALLBACK", approval_state="PENDING", round_count=2, model_call_count=6,
        fallback_reason=outcome.fallback_reason, search_bounds=outcome.search_bounds.model_dump(),
        baseline_b0=payload["candidate"], candidates=shortlist(snap, ["L600-G2-route", "L960-G1-long"]),
        votes={"ATC": {"decision": "ACCEPT", "plan_hash": "(proposal L960-G1-long)", "rationale": "Accepted."},
               "PILOT": {"decision": "REJECT", "plan_hash": "(proposal L960-G1-long)", "rationale": "Illustrative dissent."},
               "GROUND": {"decision": "ACCEPT", "plan_hash": "(proposal L960-G1-long)", "rationale": "Accepted."}},
        events=events(
            ("RUN_CREATED", "system", "Run created"), ("VOTE_RECORDED", "PILOT", "REJECT"),
            ("FALLBACK_STARTED", "coordinator", outcome.fallback_reason),
            ("FALLBACK_SELECTED", "system", f"Earliest feasible plan {outcome.plan.candidate_id}; 0 model calls"),
        ),
    )
    return payload


def escalation(run_id, mutate, reason):
    snap = snapshot(mutate)
    outcome = select_fallback(snap, reason)
    payload = base_payload(snap, run_id)
    payload.update(
        negotiation_outcome=outcome.status, approval_state="NOT_APPLICABLE", fallback_reason=reason,
        blockers=outcome.blockers, missing_facts=outcome.missing_facts,
        search_bounds=outcome.search_bounds.model_dump() if outcome.search_bounds else None,
        events=events(("RUN_CREATED", "system", "Run created"), ("ESCALATED", "system", outcome.status)),
    )
    return payload


def reviewed(run_id, decision_mutate):
    """Run the real review() against an in-memory store and show its result."""
    sys.path.insert(0, str(ROOT))
    from tests.test_approvals import MemoryStore, request  # test helper store, stands in for Developer 2's

    payload = consensus()
    payload["run_id"] = run_id
    snap = snapshot()
    plan = derive_plan(snap, 960, "G1-long")
    store = MemoryStore(snap, [new_recommendation(run_id, "D01", "FEASIBLE_CONSENSUS", plan, NOW)])
    session = ReviewerSession(reviewer_id="duty-manager-demo", token="fixture")
    result = decision_mutate(store, session, request)
    payload.update(approval_state=store.approval_state(run_id), review_result=result.model_dump(mode="json"),
                   resource_revision_after=store.resources().resource_revision)
    payload["events"] = payload["events"] + [
        {"seq": len(payload["events"]) + i + 1, "type": t, "actor": "reviewer", "summary": p.get("cause") or p.get("decision")}
        for i, (_, t, p) in enumerate(store.state["events"])
    ]
    return payload


def applied_case(store, session, request):
    return review(store, session, "fixture", request(store, store_run(store)), NOW)


def stale_case(store, session, request):
    store.state["resources"]["D01"] = snapshot(lambda raw: raw.update(resource_revision=2))
    return review(store, session, "fixture", request(store, store_run(store)), NOW)


def store_run(store):
    return next(iter(store.state["recommendations"]))


FIXTURES = {
    "consensus": consensus,
    "fallback": fallback,
    "no-feasible": lambda: escalation(
        "run-fixture-no-feasible",
        lambda raw: raw["overlay"]["fuel"].update(projected_fuel_at_earliest_kg=2000),
        "No candidate keeps the fuel reserve (injected: projected fuel 2000 kg)"),
    "insufficient-data": lambda: escalation(
        "run-fixture-insufficient",
        lambda raw: raw["overlay"]["fuel"].update(projected_fuel_at_earliest_kg=None),
        "Projected fuel missing (injected)"),
    "applied": lambda: reviewed("run-fixture-applied", applied_case),
    "stale": lambda: reviewed("run-fixture-stale", stale_case),
}

if __name__ == "__main__":
    for name, build in FIXTURES.items():
        (OUT / f"{name}.json").write_text(json.dumps(build(), indent=2, default=str) + "\n")
        print("wrote", name)
