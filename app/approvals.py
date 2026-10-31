"""Developer A's review hook: revalidate against the live simulator state and compute the update.

`Store.review` calls `review_plan` inside `BEGIN IMMEDIATE` after it has checked the
reviewer session, idempotency key, pending state, plan hash, revisions, expiry and
fallback acknowledgement. This hook adds the simulator rules (PRD 11.3 steps 5-6):

1. Overlay every plan already applied to this airport resource onto the frozen
   snapshot, so the recheck sees the current simulated runway and gate bookings.
2. Re-run every hard rule through `app.planning.check_plan` on that current state.
3. Return the checks and the resource data with this plan's bookings added.

The store refuses to commit unless every required check passes, increments the
resource revision once, and rolls everything back if this function raises. The hook
is pure: no storage writes, no network, no clock.
"""

from __future__ import annotations

import copy
from typing import Any, cast

from pydantic import JsonValue

from app import planning
from app.contracts import (
    CandidatePlan,
    Fact,
    ResourceState,
    ReviewEffect,
    ReviewRequest,
    RunRecord,
    ScenarioSnapshot,
    ServiceError,
)

APPLIED_KEY = "applied_plans"


def _applied(resource: ResourceState) -> list[dict[str, Any]]:
    entries = resource.data.get(APPLIED_KEY, [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ServiceError("RESOURCE_STATE_INVALID")
    return cast(list[dict[str, Any]], entries)


def booking(run: RunRecord, plan: CandidatePlan) -> dict[str, JsonValue]:
    """What one approved plan occupies in the simulated airport."""
    facts = {fact.fact_id: fact.value for fact in run.snapshot.facts}
    aircraft = facts.get("overlay.aircraft")
    service = facts.get("overlay.service")
    if not isinstance(aircraft, dict) or not isinstance(service, dict):
        raise ServiceError("RESOURCE_FACTS_MISSING")
    buffer = int(cast(int, service["gate_buffer_s"]))
    return {
        "booking_id": f"applied:{run.run_id}",
        "run_id": run.run_id,
        "plan_hash": plan.plan_hash,
        "candidate_id": plan.candidate_id,
        "runway_id": plan.runway_id,
        "landing_time_s": plan.landing_time_s,
        "runway_occupancy_s": plan.runway_occupancy_end_s - plan.landing_time_s,
        "wake_category": aircraft["wake_category"],
        "gate_id": plan.gate_id,
        "gate_interval_s": [plan.gate_in_time_s - buffer, plan.gate_release_s + buffer],
    }


def current_snapshot(snapshot: ScenarioSnapshot, resource: ResourceState) -> ScenarioSnapshot:
    """The frozen snapshot with every booking already applied to this resource added in.

    A revised snapshot may already contain a booking (same `booking_id`); it is not added twice.
    """
    entries = _applied(resource)
    if not entries:
        return snapshot
    facts = {fact.fact_id: fact for fact in snapshot.facts}
    # Deep copies: the frozen snapshot's nested lists must never be edited in place.
    movements: list[dict[str, Any]] = copy.deepcopy(cast(Any, facts["overlay.movements"].value))
    gates: list[dict[str, Any]] = copy.deepcopy(cast(Any, facts["overlay.gates"].value))
    known_movements = {m["movement_id"] for m in movements}
    for entry in entries:
        if entry["booking_id"] not in known_movements:
            movements.append(
                {
                    "movement_id": entry["booking_id"],
                    "time_s": entry["landing_time_s"],
                    "wake_category": entry["wake_category"],
                    "occupancy_s": entry["runway_occupancy_s"],
                }
            )
        gate = next((g for g in gates if g["gate_id"] == entry["gate_id"]), None)
        if gate is None:
            raise ServiceError("RESOURCE_STATE_INVALID")
        interval = list(entry["gate_interval_s"])
        if interval not in [list(r) for r in gate["reservations"]]:
            gate["reservations"] = [*gate["reservations"], interval]
    replaced = {
        "overlay.movements": _with_value(facts["overlay.movements"], cast(JsonValue, movements)),
        "overlay.gates": _with_value(facts["overlay.gates"], cast(JsonValue, gates)),
    }
    return snapshot.model_copy(
        update={"facts": tuple(replaced.get(f.fact_id, f) for f in snapshot.facts)}
    )


def _with_value(fact: Fact, value: JsonValue) -> Fact:
    return fact.model_copy(update={"value": value, "provenance": "derived"})


def review_plan(run: RunRecord, resource: ResourceState, request: ReviewRequest) -> ReviewEffect:
    """Recheck the persisted plan on the current simulated airport and return its bookings."""
    plan = run.candidate
    if plan is None:
        raise ServiceError("REVIEW_NOT_PENDING")
    if resource.resource_id != run.snapshot.resource_id:
        raise ServiceError("RESOURCE_MISMATCH")
    entries = _applied(resource)
    if any(entry.get("run_id") == run.run_id for entry in entries):
        raise ServiceError("ALREADY_APPLIED")
    checks = planning.check_plan(current_snapshot(run.snapshot, resource), plan)
    return ReviewEffect(
        checks=checks,
        resource_data={**resource.data, APPLIED_KEY: [*entries, booking(run, plan)]},
    )
