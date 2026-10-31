"""Deterministic non-AI fallback (PRD 8.2 steps 1-6 and 8.7). Makes zero model calls.

The coordinator decides *when* fallback runs and passes its reason; this module decides
*what* it returns. It always re-enumerates and re-checks the full bounded catalog itself,
so a stale or tampered catalog cannot leak into the result, and it never picks a
"least unsafe" candidate.
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

from app.constraints import CandidatePlan, ConstraintResult, check_plan, derive_plan, is_approval_ready
from app.scenarios import ScenarioSnapshot

FallbackStatus = Literal[
    "FEASIBLE_FALLBACK", "NO_FEASIBLE_OPTION_IN_SEARCH_SPACE", "INSUFFICIENT_DATA", "SYSTEM_ERROR",
]

# Facts every candidate needs; any null stops planning before enumeration (PRD 8.2 step 1).
CRITICAL_FACTS = (
    ("runway", "focal_occupancy_s"),
    ("aircraft", "aircraft_class"),
    ("aircraft", "wake_category"),
    ("fuel", "projected_fuel_at_earliest_kg"),
    ("fuel", "hold_burn_kg_per_min"),
    ("fuel", "taxi_burn_kg_per_min"),
    ("fuel", "reserve_floor_kg"),
    ("fuel", "uncertainty_allowance_kg"),
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CheckedCandidate(_Frozen):
    plan: CandidatePlan
    checks: list[ConstraintResult]
    feasible: bool


class SearchBounds(_Frozen):
    earliest_landing_s: int
    latest_landing_s: int
    step_s: int
    route_ids: list[str]
    raw_candidate_count: int


class FallbackResult(_Frozen):
    status: FallbackStatus
    fallback_reason: str
    plan: Optional[CandidatePlan]
    checks: list[ConstraintResult]
    search_bounds: Optional[SearchBounds]
    evaluated_count: int
    feasible_count: int
    blockers: dict[str, int]  # rule_id -> candidates it failed or left unknown
    missing_facts: list[str]
    model_calls: int = 0


def missing_critical_facts(snapshot: ScenarioSnapshot) -> list[str]:
    return [
        f"{section}.{field}"
        for section, field in CRITICAL_FACTS
        if getattr(getattr(snapshot.overlay, section), field) is None
    ]


def search_bounds(snapshot: ScenarioSnapshot) -> SearchBounds:
    search = snapshot.overlay.search
    slots = len(range(search.earliest_landing_s, search.earliest_landing_s + search.horizon_s + 1, search.step_s))
    route_ids = [route.route_id for route in sorted(snapshot.overlay.routes, key=lambda r: (r.gate_id, r.route_id))]
    return SearchBounds(
        earliest_landing_s=search.earliest_landing_s,
        latest_landing_s=search.earliest_landing_s + search.horizon_s,
        step_s=search.step_s,
        route_ids=route_ids,
        raw_candidate_count=slots * len(route_ids),
    )


def enumerate_candidates(snapshot: ScenarioSnapshot) -> list[CandidatePlan]:
    """Every landing slot (inclusive horizon, fixed step) x every declared route, in stable order."""
    bounds = search_bounds(snapshot)
    return [
        derive_plan(snapshot, landing, route_id)
        for landing in range(bounds.earliest_landing_s, bounds.latest_landing_s + 1, bounds.step_s)
        for route_id in bounds.route_ids
    ]


def evaluate_catalog(snapshot: ScenarioSnapshot) -> list[CheckedCandidate]:
    checked = []
    for plan in enumerate_candidates(snapshot):
        checks = check_plan(snapshot, plan)
        checked.append(CheckedCandidate(plan=plan, checks=checks, feasible=is_approval_ready(checks)))
    return checked


def procedural_order(plan: CandidatePlan) -> tuple:
    """PRD 8.7 ordering, shared by fallback and the B0 procedural baseline."""
    return (plan.landing_time_s, plan.gate_in_time_s, plan.gate_id, plan.taxi_route_id, plan.candidate_id)


def first_feasible(catalog: list[CheckedCandidate]) -> Optional[CheckedCandidate]:
    feasible = [candidate for candidate in catalog if candidate.feasible]
    return min(feasible, key=lambda c: procedural_order(c.plan)) if feasible else None


def _blockers(catalog: list[CheckedCandidate]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in catalog:
        for check in candidate.checks:
            if check.status != "PASS":
                counts[check.rule_id] = counts.get(check.rule_id, 0) + 1
    return dict(sorted(counts.items()))


def select_fallback(snapshot: ScenarioSnapshot, fallback_reason: str) -> FallbackResult:
    """Earliest feasible plan under the full hard-rule set, or a typed non-executable outcome."""
    base = {"fallback_reason": fallback_reason, "plan": None, "checks": [], "search_bounds": None,
            "evaluated_count": 0, "feasible_count": 0, "blockers": {}, "missing_facts": []}
    missing = missing_critical_facts(snapshot)
    if missing:
        return FallbackResult(**{**base, "status": "INSUFFICIENT_DATA", "missing_facts": missing})
    try:
        bounds = search_bounds(snapshot)
        catalog = evaluate_catalog(snapshot)
    except Exception as error:  # a checker fault must never yield an approvable plan
        return FallbackResult(**{**base, "status": "SYSTEM_ERROR",
                                 "fallback_reason": f"{fallback_reason}; checker error: {error!r}"})

    summary = {**base, "search_bounds": bounds, "evaluated_count": len(catalog),
               "feasible_count": sum(c.feasible for c in catalog), "blockers": _blockers(catalog)}
    chosen = first_feasible(catalog)
    if chosen is not None:
        return FallbackResult(**{**summary, "status": "FEASIBLE_FALLBACK", "plan": chosen.plan,
                                 "checks": chosen.checks})
    # No feasible plan: only claim "no option" if every candidate has a known failure.
    if all(any(check.status == "FAIL" for check in candidate.checks) for candidate in catalog):
        return FallbackResult(**{**summary, "status": "NO_FEASIBLE_OPTION_IN_SEARCH_SPACE"})
    return FallbackResult(**{**summary, "status": "INSUFFICIENT_DATA"})
