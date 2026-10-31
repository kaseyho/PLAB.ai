"""Deterministic ATC, pilot and ground hard rules (PRD 8.1, 8.3-8.5). No model is involved.

`check_plan` never trusts derived numbers supplied with a plan: it re-derives timings and
fuel from the plan's choices (landing time, taxi route) and the snapshot, and reports a
SYSTEM failure if the supplied plan differs from that derivation.

All thresholds are illustrative scenario policies, not FAA/ICAO/CAAS compliance minima.
Intervals are half-open [start, end).
"""

from fractions import Fraction
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict

from app.scenarios import ScenarioSnapshot, canonical_hash

RULE_VERSION = "toy-rules-v1"

Status = Literal["PASS", "FAIL", "UNKNOWN"]
Domain = Literal["ATC", "PILOT", "GROUND", "SYSTEM"]

REQUIRED_RULES = (
    "SYSTEM-FRESH-01",
    "ATC-RWY-01", "ATC-SEP-01", "ATC-TAXI-01", "ATC-CAP-01",
    "PILOT-FUEL-01", "PILOT-ROUTE-01",
    "GROUND-GATE-01", "GROUND-OCC-01", "GROUND-CREW-01", "GROUND-EQUIP-01",
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EdgeInterval(_Frozen):
    edge_id: str
    start_s: int
    end_s: int


class CandidatePlan(_Frozen):
    candidate_id: str
    scenario_id: str
    scenario_revision: int
    resource_revision: int
    ruleset_version: str
    policy_version: str
    landing_time_s: int
    runway_id: str
    runway_occupancy_end_s: Optional[int]
    taxi_route_id: str
    taxi_duration_s: int
    route_edge_intervals: list[EdgeInterval]
    gate_id: str
    gate_in_time_s: int
    service_start_s: int
    service_end_s: int
    gate_release_s: int
    crew_id: str
    plan_hash: str


class ConstraintResult(_Frozen):
    rule_id: str
    rule_version: str
    domain: Domain
    candidate_id: str
    status: Status
    actual: Any
    operator: str
    required: Any
    units: str
    margin: Any
    evidence_ids: list[str]
    reason_code: str


def overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _exact(value: float) -> Fraction:
    # str() first so 0.1 becomes exactly 1/10 rather than its binary approximation
    return Fraction(str(value))


def _show(value: Fraction) -> int | float:
    return value.numerator if value.denominator == 1 else float(value)


def derive_plan(snapshot: ScenarioSnapshot, landing_time_s: int, taxi_route_id: str) -> CandidatePlan:
    """Canonical plan from the two free choices. Raises KeyError for an undeclared route."""
    overlay = snapshot.overlay
    routes = {route.route_id: route for route in overlay.routes}
    if taxi_route_id not in routes:
        raise KeyError(f"route {taxi_route_id!r} is not declared in scenario {snapshot.scenario_id}")
    route = routes[taxi_route_id]
    gate = overlay.gate(route.gate_id)
    service = overlay.service

    edges, cursor = [], landing_time_s
    for edge in route.edges:
        edges.append(EdgeInterval(edge_id=edge.edge_id, start_s=cursor, end_s=cursor + edge.duration_s))
        cursor += edge.duration_s
    gate_in = landing_time_s + route.taxi_duration_s
    service_start = gate_in + service.pre_service_buffer_s
    service_end = service_start + service.duration_s
    occupancy = overlay.runway.focal_occupancy_s

    body = {
        "candidate_id": f"L{landing_time_s}-{taxi_route_id}",
        "scenario_id": snapshot.scenario_id,
        "scenario_revision": snapshot.scenario_revision,
        "resource_revision": snapshot.resource_revision,
        "ruleset_version": snapshot.ruleset_version,
        "policy_version": snapshot.policy_version,
        "landing_time_s": landing_time_s,
        "runway_id": overlay.runway.runway_id,
        "runway_occupancy_end_s": None if occupancy is None else landing_time_s + occupancy,
        "taxi_route_id": taxi_route_id,
        "taxi_duration_s": route.taxi_duration_s,
        "route_edge_intervals": edges,
        "gate_id": route.gate_id,
        "gate_in_time_s": gate_in,
        "service_start_s": service_start,
        "service_end_s": service_end,
        "gate_release_s": service_end + service.release_buffer_s,
        "crew_id": gate.crew_id,
    }
    hashed = {**body, "route_edge_intervals": [edge.model_dump() for edge in edges]}
    return CandidatePlan(**body, plan_hash=canonical_hash(hashed))


def fuel_at_gate_kg(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> Optional[Fraction]:
    """PRD 8.5 fuel proxy; None when any input is missing. F_e already covers the journey to e."""
    fuel = snapshot.overlay.fuel
    if None in (fuel.projected_fuel_at_earliest_kg, fuel.hold_burn_kg_per_min, fuel.taxi_burn_kg_per_min):
        return None
    hold_min = Fraction(plan.landing_time_s - snapshot.overlay.search.earliest_landing_s, 60)
    taxi_min = Fraction(plan.taxi_duration_s, 60)
    return (
        _exact(fuel.projected_fuel_at_earliest_kg)
        - _exact(fuel.hold_burn_kg_per_min) * hold_min
        - _exact(fuel.taxi_burn_kg_per_min) * taxi_min
    )


def _result(rule_id: str, domain: Domain, plan: CandidatePlan, status: Status, **fields) -> ConstraintResult:
    defaults = {"actual": None, "operator": "", "required": None, "units": "", "margin": None,
                "evidence_ids": [], "reason_code": "OK" if status == "PASS" else status}
    return ConstraintResult(rule_id=rule_id, rule_version=RULE_VERSION, domain=domain,
                            candidate_id=plan.candidate_id, status=status, **{**defaults, **fields})


def check_freshness(snapshot: ScenarioSnapshot, supplied: CandidatePlan, canonical: CandidatePlan) -> ConstraintResult:
    expected = {
        "scenario_id": snapshot.scenario_id,
        "scenario_revision": snapshot.scenario_revision,
        "resource_revision": snapshot.resource_revision,
        "ruleset_version": snapshot.ruleset_version,
        "policy_version": snapshot.policy_version,
    }
    actual = {key: getattr(supplied, key) for key in expected}
    fields = {"actual": actual, "operator": "==", "required": expected, "units": "version",
              "evidence_ids": ["snapshot.versions", f"plan.{supplied.candidate_id}.plan_hash"]}
    if actual != expected:
        return _result("SYSTEM-FRESH-01", "SYSTEM", supplied, "FAIL", **fields, reason_code="VERSION_MISMATCH")
    if supplied.plan_hash != canonical.plan_hash or supplied != canonical:
        return _result("SYSTEM-FRESH-01", "SYSTEM", supplied, "FAIL", **fields, reason_code="PLAN_NOT_CANONICAL")
    return _result("SYSTEM-FRESH-01", "SYSTEM", supplied, "PASS", **fields)


def check_runway(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> ConstraintResult:
    overlay = snapshot.overlay
    evidence = ["overlay.runway.available_intervals", "overlay.runway.focal_occupancy_s", "overlay.movements"]
    if plan.runway_occupancy_end_s is None:
        return _result("ATC-RWY-01", "ATC", plan, "UNKNOWN", evidence_ids=evidence, reason_code="MISSING_OCCUPANCY")
    occupancy = (plan.landing_time_s, plan.runway_occupancy_end_s)
    fields = {"actual": list(occupancy), "operator": "inside available, no overlap", "units": "s",
              "evidence_ids": evidence}
    available = [list(i) for i in overlay.runway.available_intervals]
    if not any(start <= occupancy[0] and occupancy[1] <= end for start, end in overlay.runway.available_intervals):
        return _result("ATC-RWY-01", "ATC", plan, "FAIL", **fields, required=available, reason_code="RUNWAY_UNAVAILABLE")
    clashes = [m.movement_id for m in overlay.movements if overlap(occupancy, (m.time_s, m.time_s + m.occupancy_s))]
    if clashes:
        fields["evidence_ids"] = evidence + [f"overlay.movements.{c}" for c in clashes]
        return _result("ATC-RWY-01", "ATC", plan, "FAIL", **fields, required=available, reason_code="RUNWAY_OCCUPIED")
    return _result("ATC-RWY-01", "ATC", plan, "PASS", **fields, required=available)


def check_separation(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> ConstraintResult:
    overlay = snapshot.overlay
    focal = overlay.aircraft.wake_category
    landing = plan.landing_time_s
    evidence = ["overlay.aircraft.wake_category", "overlay.separation_s"]
    if focal is None:
        return _result("ATC-SEP-01", "ATC", plan, "UNKNOWN", evidence_ids=evidence, reason_code="MISSING_WAKE_CATEGORY")
    preceding = [m for m in overlay.movements if m.time_s <= landing]
    following = [m for m in overlay.movements if m.time_s > landing]
    neighbours = []
    if preceding:
        lead = max(preceding, key=lambda m: (m.time_s, m.movement_id))
        neighbours.append((lead, lead.wake_category, focal, landing - lead.time_s))
    if following:
        follow = min(following, key=lambda m: (m.time_s, m.movement_id))
        neighbours.append((follow, focal, follow.wake_category, follow.time_s - landing))

    actual, required, margins = {}, {}, []
    for movement, leader, follower, spacing in neighbours:
        evidence.append(f"overlay.movements.{movement.movement_id}")
        key = f"{leader}>{follower}"
        if leader is None or follower is None or key not in overlay.separation_s:
            return _result("ATC-SEP-01", "ATC", plan, "UNKNOWN", evidence_ids=evidence,
                           reason_code="SEPARATION_NOT_APPLICABLE")
        actual[movement.movement_id] = spacing
        required[movement.movement_id] = overlay.separation_s[key]
        margins.append(spacing - overlay.separation_s[key])
    fields = {"actual": actual, "operator": ">=", "required": required, "units": "s",
              "margin": min(margins) if margins else None, "evidence_ids": evidence}
    if margins and min(margins) < 0:
        return _result("ATC-SEP-01", "ATC", plan, "FAIL", **fields, reason_code="SEPARATION_BELOW_MINIMUM")
    return _result("ATC-SEP-01", "ATC", plan, "PASS", **fields)


def check_taxi(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> ConstraintResult:
    overlay = snapshot.overlay
    route = overlay.route(plan.taxi_route_id)
    evidence = [f"overlay.routes.{route.route_id}", "overlay.taxi_blockages"]
    actual = [[e.edge_id, e.start_s, e.end_s] for e in plan.route_edge_intervals]
    fields = {"actual": actual, "operator": "no edge overlaps a blockage", "units": "s", "evidence_ids": evidence}
    if route.gate_id != plan.gate_id:
        return _result("ATC-TAXI-01", "ATC", plan, "FAIL", **fields, required=route.gate_id,
                       reason_code="ROUTE_GATE_MISMATCH")
    blocked = sorted({
        edge.edge_id
        for edge in plan.route_edge_intervals
        for block in overlay.taxi_blockages.get(edge.edge_id, [])
        if overlap((edge.start_s, edge.end_s), block)
    })
    if blocked:
        return _result("ATC-TAXI-01", "ATC", plan, "FAIL", **fields, required={"blocked_edges": blocked},
                       reason_code="TAXI_EDGE_BLOCKED")
    return _result("ATC-TAXI-01", "ATC", plan, "PASS", **fields)


def check_capacity(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> ConstraintResult:
    """Aligned windows [k*W, (k+1)*W) from the epoch; the focal movement counts as one."""
    capacity = snapshot.overlay.capacity
    window = plan.landing_time_s // capacity.window_s
    count = 1 + sum(1 for m in snapshot.overlay.movements if m.time_s // capacity.window_s == window)
    fields = {"actual": count, "operator": "<=", "required": capacity.max_movements, "units": "movements",
              "margin": capacity.max_movements - count,
              "evidence_ids": ["overlay.capacity", "overlay.movements"]}
    if count > capacity.max_movements:
        return _result("ATC-CAP-01", "ATC", plan, "FAIL", **fields, reason_code="CAPACITY_EXCEEDED")
    return _result("ATC-CAP-01", "ATC", plan, "PASS", **fields)


def check_fuel(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> ConstraintResult:
    fuel = snapshot.overlay.fuel
    evidence = ["overlay.fuel", "overlay.search.earliest_landing_s", f"overlay.routes.{plan.taxi_route_id}"]
    at_gate = fuel_at_gate_kg(snapshot, plan)
    if at_gate is None or fuel.reserve_floor_kg is None or fuel.uncertainty_allowance_kg is None:
        return _result("PILOT-FUEL-01", "PILOT", plan, "UNKNOWN", evidence_ids=evidence, reason_code="MISSING_FUEL_INPUT")
    required = _exact(fuel.reserve_floor_kg) + _exact(fuel.uncertainty_allowance_kg)
    margin = at_gate - required
    fields = {"actual": _show(at_gate), "operator": ">=", "required": _show(required), "units": "kg",
              "margin": _show(margin), "evidence_ids": evidence}
    if plan.landing_time_s < snapshot.overlay.search.earliest_landing_s:
        return _result("PILOT-FUEL-01", "PILOT", plan, "FAIL", **fields, reason_code="LANDING_BEFORE_EARLIEST")
    if margin < 0:
        return _result("PILOT-FUEL-01", "PILOT", plan, "FAIL", **fields, reason_code="BELOW_RESERVE_PLUS_ALLOWANCE")
    return _result("PILOT-FUEL-01", "PILOT", plan, "PASS", **fields)


def check_arrival_option(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> ConstraintResult:
    covering = [o for o in snapshot.overlay.arrival_options
                if o.landing_window[0] <= plan.landing_time_s < o.landing_window[1]]
    fields = {"actual": {o.arrival_option_id: o.permitted for o in covering}, "operator": "all permitted",
              "required": True, "units": "label",
              "evidence_ids": [f"overlay.arrival_options.{o.arrival_option_id}" for o in covering] or ["overlay.arrival_options"]}
    if any(o.permitted is False for o in covering):
        return _result("PILOT-ROUTE-01", "PILOT", plan, "FAIL", **fields, reason_code="ARRIVAL_OPTION_BLOCKED")
    if not covering:
        return _result("PILOT-ROUTE-01", "PILOT", plan, "UNKNOWN", **fields, reason_code="NO_ARRIVAL_OPTION")
    if any(o.permitted is None for o in covering):
        return _result("PILOT-ROUTE-01", "PILOT", plan, "UNKNOWN", **fields, reason_code="PERMISSION_UNKNOWN")
    return _result("PILOT-ROUTE-01", "PILOT", plan, "PASS", **fields)


def check_gate_compatibility(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> ConstraintResult:
    gate = snapshot.overlay.gate(plan.gate_id)
    aircraft_class = snapshot.overlay.aircraft.aircraft_class
    fields = {"actual": aircraft_class, "operator": "in", "required": gate.compatible_classes, "units": "class",
              "evidence_ids": ["overlay.aircraft.aircraft_class", f"overlay.gates.{gate.gate_id}.compatible_classes"]}
    if aircraft_class is None:
        return _result("GROUND-GATE-01", "GROUND", plan, "UNKNOWN", **fields, reason_code="MISSING_AIRCRAFT_CLASS")
    if aircraft_class not in gate.compatible_classes:
        return _result("GROUND-GATE-01", "GROUND", plan, "FAIL", **fields, reason_code="GATE_INCOMPATIBLE")
    return _result("GROUND-GATE-01", "GROUND", plan, "PASS", **fields)


def check_gate_occupancy(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> ConstraintResult:
    gate = snapshot.overlay.gate(plan.gate_id)
    buffer = snapshot.overlay.service.gate_buffer_s
    occupied = (plan.gate_in_time_s - buffer, plan.gate_release_s + buffer)
    clashes = [list(r) for r in gate.reservations if overlap(occupied, r)]
    fields = {"actual": list(occupied), "operator": "no overlap", "required": [list(r) for r in gate.reservations],
              "units": "s", "evidence_ids": [f"overlay.gates.{gate.gate_id}.reservations", "overlay.service"]}
    if clashes:
        return _result("GROUND-OCC-01", "GROUND", plan, "FAIL", **fields, reason_code="GATE_OCCUPIED")
    return _result("GROUND-OCC-01", "GROUND", plan, "PASS", **fields)


def check_crew(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> ConstraintResult:
    """Hard limit is availability and maximum duty end; overtime before that is a cost, not a failure."""
    crew = snapshot.overlay.crew(plan.crew_id)
    fields = {"actual": [plan.service_start_s, plan.service_end_s], "operator": "within",
              "required": [crew.available_from_s, crew.max_duty_end_s], "units": "s",
              "margin": crew.max_duty_end_s - plan.service_end_s,
              "evidence_ids": [f"overlay.crews.{crew.crew_id}", "overlay.service"]}
    if plan.service_start_s < crew.available_from_s:
        return _result("GROUND-CREW-01", "GROUND", plan, "FAIL", **fields, reason_code="CREW_NOT_AVAILABLE")
    if plan.service_end_s > crew.max_duty_end_s:
        return _result("GROUND-CREW-01", "GROUND", plan, "FAIL", **fields, reason_code="EXCEEDS_MAX_DUTY")
    return _result("GROUND-CREW-01", "GROUND", plan, "PASS", **fields)


def check_equipment(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> ConstraintResult:
    overlay = snapshot.overlay
    gate = overlay.gate(plan.gate_id)
    needed = (plan.service_start_s, plan.service_end_s)
    calendars = {item.equipment_id: item.available_intervals for item in overlay.equipment}
    unavailable = [
        equipment_id for equipment_id in gate.required_equipment
        if not any(start <= needed[0] and needed[1] <= end for start, end in calendars[equipment_id])
    ]
    fields = {"actual": list(needed), "operator": "inside an available interval", "required": gate.required_equipment,
              "units": "s", "evidence_ids": [f"overlay.equipment.{e}" for e in gate.required_equipment]}
    if unavailable:
        return _result("GROUND-EQUIP-01", "GROUND", plan, "FAIL", **fields, reason_code="EQUIPMENT_UNAVAILABLE")
    return _result("GROUND-EQUIP-01", "GROUND", plan, "PASS", **fields)


def check_plan(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> list[ConstraintResult]:
    """All required rule results, in REQUIRED_RULES order. Any FAIL/UNKNOWN blocks review."""
    try:
        canonical = derive_plan(snapshot, plan.landing_time_s, plan.taxi_route_id)
    except KeyError:
        unknown_route = _result("SYSTEM-FRESH-01", "SYSTEM", plan, "FAIL", actual=plan.taxi_route_id,
                                operator="declared route", required=[r.route_id for r in snapshot.overlay.routes],
                                units="route", evidence_ids=["overlay.routes"], reason_code="UNDECLARED_ROUTE")
        return [unknown_route]
    rules = (check_runway, check_separation, check_taxi, check_capacity, check_fuel, check_arrival_option,
             check_gate_compatibility, check_gate_occupancy, check_crew, check_equipment)
    return [check_freshness(snapshot, plan, canonical)] + [rule(snapshot, canonical) for rule in rules]


def is_approval_ready(results: list[ConstraintResult]) -> bool:
    """True only when every required rule is present exactly once and every one passed."""
    statuses = {result.rule_id: result.status for result in results}
    return len(results) == len(REQUIRED_RULES) and set(statuses) == set(REQUIRED_RULES) and all(
        status == "PASS" for status in statuses.values()
    )
