"""Pure simulator review hook, called inside Store.review's SQLite transaction.

The API/store own authentication, idempotency, expiry and atomic persistence.
This module rechecks current reservations and computes runway/gate reservations.
"""

from typing import cast

from pydantic import JsonValue, TypeAdapter, ValidationError, model_validator

from app.contracts import (
    Contract,
    Count,
    ResourceState,
    ReviewEffect,
    ReviewRequest,
    RunRecord,
    ScenarioSnapshot,
    ServiceError,
    require_passing_checks,
)
from app.planning import check_plan
from app.scenarios import Movement, Overlay

APPLIED_KEY = "reservations"


class Reservation(Contract):
    run_id: str
    plan_hash: str | None = None
    candidate_id: str | None = None
    runway_id: str
    movement: Movement
    gate_id: str
    gate_interval: tuple[Count, Count]
    crew_id: str | None = None
    equipment_ids: tuple[str, ...] = ()
    service_interval: tuple[Count, Count] | None = None

    @model_validator(mode="after")
    def valid_interval(self) -> "Reservation":
        if self.gate_interval[0] >= self.gate_interval[1]:
            raise ValueError("A gate reservation must have positive duration")
        if self.service_interval and self.service_interval[0] >= self.service_interval[1]:
            raise ValueError("A service reservation must have positive duration")
        if (self.crew_id is None) != (self.service_interval is None):
            raise ValueError("Crew and service interval must be supplied together")
        return self


def reservations(resource: ResourceState) -> list[Reservation]:
    try:
        return TypeAdapter(list[Reservation]).validate_python(resource.data.get(APPLIED_KEY, []))
    except ValidationError as error:
        raise ServiceError("RESOURCE_STATE_INVALID") from error


def current_snapshot(snapshot: ScenarioSnapshot, resource: ResourceState) -> ScenarioSnapshot:
    """Include committed runway/gate reservations in a proposed or reviewed snapshot.

    Reservations are retained independently of editable scenario facts. Revisions
    therefore cannot accidentally erase a previously applied movement or booking.
    """
    if snapshot.resource_id != resource.resource_id:
        raise ServiceError("RESOURCE_VERSION_MISMATCH")
    facts = {fact.fact_id: fact for fact in snapshot.facts}
    if "overlay.movements" not in facts:
        return snapshot  # Explicit backend fixtures use their own review hook.
    overlay = Overlay.model_validate(
        {
            name: facts[f"overlay.{name}"].value
            for name in Overlay.model_fields
            if f"overlay.{name}" in facts
        }
    )
    movements = {movement.movement_id: movement for movement in overlay.movements}
    gates = {gate.gate_id: gate for gate in overlay.gates}
    crews = {crew.crew_id: crew for crew in overlay.crews}
    equipment = {item.equipment_id: item for item in overlay.equipment}
    committed = reservations(resource)
    for reservation in committed:
        if reservation.gate_id not in gates:
            raise ServiceError("RESOURCE_STATE_INVALID")
        movements[reservation.movement.movement_id] = reservation.movement.model_copy(
            update={"runway_id": reservation.runway_id}
        )
        gate = gates[reservation.gate_id]
        intervals = sorted(set(gate.reservations) | {reservation.gate_interval})
        gates[gate.gate_id] = gate.model_copy(update={"reservations": intervals})
        if reservation.crew_id is not None and reservation.service_interval is not None:
            if reservation.crew_id not in crews or any(
                key not in equipment for key in reservation.equipment_ids
            ):
                raise ServiceError("RESOURCE_STATE_INVALID")
            crew = crews[reservation.crew_id]
            crews[crew.crew_id] = crew.model_copy(
                update={
                    "reservations": sorted(set(crew.reservations) | {reservation.service_interval})
                }
            )
            start, end = reservation.service_interval
            for key in reservation.equipment_ids:
                available = []
                for left, right in equipment[key].available_intervals:
                    if right <= start or end <= left:
                        available.append((left, right))
                    else:
                        if left < start:
                            available.append((left, start))
                        if end < right:
                            available.append((end, right))
                equipment[key] = equipment[key].model_copy(
                    update={"available_intervals": available}
                )
    if committed:
        for section, values in (
            ("movements", movements.values()),
            ("gates", gates.values()),
            ("crews", crews.values()),
            ("equipment", equipment.values()),
        ):
            fact_id = f"overlay.{section}"
            facts[fact_id] = facts[fact_id].model_copy(
                update={
                    "value": [item.model_dump(mode="json") for item in values],
                    "provenance": "derived",
                    "source_reference": f"resource:{resource.resource_id}/reservations",
                    "source_version": f"revision-{resource.revision}",
                }
            )
    versions = facts["snapshot.versions"]
    facts[versions.fact_id] = versions.model_copy(
        update={
            "value": {
                "scenario_revision": snapshot.revision,
                "resource_revision": snapshot.resource_revision,
                "ruleset_version": snapshot.ruleset_version,
                "policy_version": snapshot.policy_version,
            }
        }
    )
    return snapshot.model_copy(update={"facts": tuple(facts.values())})


def review_plan(run: RunRecord, resource: ResourceState, request: ReviewRequest) -> ReviewEffect:
    plan = run.candidate
    if plan is None or request.decision != "APPROVE":
        raise ServiceError("NOT_REVIEWABLE")
    if resource.revision != run.snapshot.resource_revision:
        raise ServiceError("RESOURCE_VERSION_MISMATCH")
    booked = reservations(resource)
    if any(reservation.run_id == run.run_id for reservation in booked):
        raise ServiceError("ALREADY_APPLIED")
    snapshot = current_snapshot(run.snapshot, resource)
    checks = check_plan(snapshot, plan)
    require_passing_checks(snapshot, plan, checks)
    facts = {fact.fact_id: fact for fact in snapshot.facts}
    overlay = Overlay.model_validate(
        {
            name: facts[f"overlay.{name}"].value
            for name in Overlay.model_fields
            if f"overlay.{name}" in facts
        }
    )
    buffer = overlay.service.gate_buffer_s
    reservation = Reservation(
        run_id=run.run_id,
        plan_hash=plan.plan_hash,
        candidate_id=plan.candidate_id,
        runway_id=plan.runway_id,
        movement=Movement(
            movement_id=f"APPLIED-{run.run_id}",
            time_s=plan.landing_time_s,
            wake_category=overlay.aircraft.wake_category,
            occupancy_s=plan.runway_occupancy_end_s - plan.landing_time_s,
            runway_id=plan.runway_id,
        ),
        gate_id=plan.gate_id,
        gate_interval=(max(0, plan.gate_in_time_s - buffer), plan.gate_release_s + buffer),
        crew_id=plan.crew_id,
        equipment_ids=tuple(overlay.gate(plan.gate_id).required_equipment),
        service_interval=(plan.service_start_s, plan.service_end_s),
    )
    booked.append(reservation)
    return ReviewEffect(
        checks=checks,
        resource_data={
            **resource.data,
            APPLIED_KEY: cast(JsonValue, [item.model_dump(mode="json") for item in booked]),
        },
    )
