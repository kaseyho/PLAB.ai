"""Connect PR #1's operational checker to the shared run and API contracts.

Overlay files remain Developer C's typed input format. Runs store their frozen
facts and actual source row; all agent-facing plans and checks use contracts.py.
"""

from __future__ import annotations

import csv
import hashlib
import json
from decimal import Decimal
from fractions import Fraction
from itertools import islice
from pathlib import Path

from app import constraints, fallback, scenarios
from app.contracts import (
    CandidatePlan,
    CatalogResult,
    ConstraintResult,
    DomainCosts,
    EvaluatedCandidate,
    Fact,
    FallbackResult,
    HistoricalRecord,
    ImpactVector,
    Role,
    RouteEdge,
    ScenarioSnapshot,
    ServiceError,
)

DOMAINS: dict[str, Role | None] = {
    "search": None,
    "aircraft": None,
    "runway": Role.ATC,
    "movements": Role.ATC,
    "separation_s": Role.ATC,
    "capacity": Role.ATC,
    "routes": None,
    "taxi_blockages": Role.ATC,
    "gates": Role.GROUND,
    "crews": Role.GROUND,
    "equipment": Role.GROUND,
    "fuel": Role.PILOT,
    "arrival_options": Role.PILOT,
    "service": Role.GROUND,
    "connections": Role.PILOT,
}
POLICY_ID = "balanced-v1"


def load_snapshot(path: Path, data_dir: Path) -> ScenarioSnapshot:
    raw = json.loads(path.read_text())
    if "overlay" not in raw:
        return ScenarioSnapshot.model_validate(raw)
    definition = scenarios.snapshot_from_dict(raw)
    source_path = (data_dir / definition.source.dataset_file).resolve()
    if not source_path.is_relative_to(data_dir.resolve()):
        raise ServiceError("SOURCE_PATH_OUTSIDE_DATA_DIRECTORY", 422)
    if not source_path.is_file():
        raise ServiceError("SOURCE_FILE_NOT_FOUND", 503)
    with source_path.open(newline="") as source_file:
        row = next(
            islice(
                csv.DictReader(source_file),
                definition.source.record_ordinal - 1,
                definition.source.record_ordinal,
            ),
            None,
        )
    if row is None:
        raise ServiceError("SOURCE_RECORD_NOT_FOUND", 422)
    with source_path.open("rb") as source_file:
        sha256 = hashlib.file_digest(source_file, "sha256").hexdigest()
    source = HistoricalRecord(
        dataset_id=source_path.name,
        dataset_sha256=sha256,
        record_ordinal=definition.source.record_ordinal,
        identity=f"{row.get('YEAR')}-{row.get('MONTH')}-{row.get('DAY')}:"
        f"{row.get('AIRLINE')}{row.get('FLIGHT_NUMBER')}",
        original_fields=row,
    )
    return snapshot_from_definition(definition, source)


def snapshot_from_definition(
    definition: scenarios.ScenarioSnapshot, source: HistoricalRecord
) -> ScenarioSnapshot:
    if definition.policy_version != POLICY_ID:
        raise ServiceError("UNSUPPORTED_OBJECTIVE_POLICY", 422)
    overlay = definition.overlay.model_dump(mode="json")
    facts = [
        Fact(
            fact_id=f"overlay.{section}",
            domain=domain,
            value=overlay[section],
            units="schema-defined seconds/kg/minutes",
            provenance=definition.provenance[section],
            source_reference=f"scenarios/{definition.scenario_id}.json#/overlay/{section}",
            source_version=definition.snapshot_hash,
        )
        for section, domain in DOMAINS.items()
    ]
    facts.extend(
        (
            Fact(
                fact_id="snapshot.versions",
                domain=None,
                units="version",
                provenance="derived",
                source_reference="frozen scenario envelope",
                source_version=definition.snapshot_hash,
                value={
                    "scenario_revision": definition.scenario_revision,
                    "resource_revision": definition.resource_revision,
                    "ruleset_version": definition.ruleset_version,
                    "policy_version": definition.policy_version,
                },
            ),
            Fact(
                fact_id="policy.fuel_limits",
                domain=Role.PILOT,
                units="kg",
                provenance="policy",
                source_reference="scenario fuel reserve and uncertainty policy",
                source_version=definition.ruleset_version,
                value={
                    "reserve_floor_kg": definition.overlay.fuel.reserve_floor_kg,
                    "uncertainty_allowance_kg": definition.overlay.fuel.uncertainty_allowance_kg,
                },
            ),
            Fact(
                fact_id="policy.scoring",
                domain=None,
                units="dimensionless",
                provenance="policy",
                source_reference="docs/technical_prd.md section 8.6",
                source_version=POLICY_ID,
                value={
                    "delay_normalizer_s": 1800,
                    "fuel_normalizer_kg": 800,
                    "connection_normalizer": 20,
                    "overtime_normalizer_min": 20,
                    "turnaround_normalizer_min": 30,
                    "paired_weight": "0.5",
                },
            ),
        )
    )
    return ScenarioSnapshot.model_validate(
        {
            "scenario_id": definition.scenario_id,
            "revision": definition.scenario_revision,
            "resource_id": "airport:"
            + (source.original_fields.get("DESTINATION_AIRPORT") or "unknown"),
            "resource_revision": definition.resource_revision,
            "ruleset_version": definition.ruleset_version,
            "policy_version": definition.policy_version,
            "simulation_epoch": definition.simulation_epoch_utc,
            "source": source,
            "facts": facts,
            "required_fact_ids": [f.fact_id for f in facts],
            "concession_delta": {role: "0.25" for role in Role},
        }
    )


def _definition(snapshot: ScenarioSnapshot) -> scenarios.ScenarioSnapshot:
    if snapshot.policy_version != POLICY_ID:
        raise ServiceError("UNSUPPORTED_OBJECTIVE_POLICY")
    facts = {fact.fact_id: fact for fact in snapshot.facts}
    if facts["snapshot.versions"].value != {
        "scenario_revision": snapshot.revision,
        "resource_revision": snapshot.resource_revision,
        "ruleset_version": snapshot.ruleset_version,
        "policy_version": snapshot.policy_version,
    }:
        raise ServiceError("VERSION_EVIDENCE_MISMATCH")
    raw = {
        "scenario_id": snapshot.scenario_id,
        "scenario_revision": snapshot.revision,
        "resource_revision": snapshot.resource_revision,
        "ruleset_version": snapshot.ruleset_version,
        "policy_version": snapshot.policy_version,
        "simulation_epoch_utc": snapshot.simulation_epoch.isoformat(),
        "description": "Frozen operational overlay",
        "source": {
            "dataset_file": snapshot.source.dataset_id,
            "record_ordinal": snapshot.source.record_ordinal,
        },
        "overlay": {section: facts[f"overlay.{section}"].value for section in DOMAINS},
        "provenance": {section: facts[f"overlay.{section}"].provenance for section in DOMAINS},
    }
    definition = scenarios.snapshot_from_dict(raw)
    fuel = definition.overlay.fuel
    if facts["policy.fuel_limits"].value != {
        "reserve_floor_kg": fuel.reserve_floor_kg,
        "uncertainty_allowance_kg": fuel.uncertainty_allowance_kg,
    }:
        raise ServiceError("FUEL_POLICY_MISMATCH")
    return definition


def _decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


def _plan(
    definition: scenarios.ScenarioSnapshot, native: constraints.CandidatePlan
) -> CandidatePlan:
    overlay = definition.overlay
    fuel_at_gate = constraints.fuel_at_gate_kg(definition, native)
    fuel = overlay.fuel
    if (
        fuel_at_gate is None
        or fuel.projected_fuel_at_earliest_kg is None
        or fuel.reserve_floor_kg is None
        or fuel.uncertainty_allowance_kg is None
        or native.runway_occupancy_end_s is None
        or overlay.connections is None
    ):
        raise ServiceError("MISSING_SCORING_INPUT")
    used = Fraction(str(fuel.projected_fuel_at_earliest_kg)) - fuel_at_gate
    margin = (
        fuel_at_gate
        - Fraction(str(fuel.reserve_floor_kg))
        - Fraction(str(fuel.uncertainty_allowance_kg))
    )
    regular_end = overlay.crew(native.crew_id).regular_shift_end_s
    overtime = Fraction(max(0, native.service_end_s - max(native.service_start_s, regular_end)), 60)
    lateness = Fraction(max(0, native.service_end_s - overlay.service.target_service_end_s), 60)
    missed = sum(
        group.passengers
        for group in overlay.connections
        if native.gate_in_time_s + group.transfer_time_s > group.deadline_s
    )
    delay = native.landing_time_s - overlay.search.earliest_landing_s
    covering = sorted(
        option.arrival_option_id
        for option in overlay.arrival_options
        if option.landing_window[0] <= native.landing_time_s < option.landing_window[1]
    )
    return CandidatePlan.model_validate(
        {
            **native.model_dump(exclude={"taxi_duration_s", "plan_hash"}),
            "arrival_option_id": covering[0] if covering else "UNRESOLVED",
            "route_edge_intervals": [
                RouteEdge.model_validate(e.model_dump()) for e in native.route_edge_intervals
            ],
            "impacts": ImpactVector(
                additional_landing_delay_s=delay,
                modeled_fuel_used_kg=_decimal(used),
                fuel_at_gate_kg=_decimal(fuel_at_gate),
                reserve_margin_kg=_decimal(margin),
                crew_overtime_min=_decimal(overtime),
                turnaround_lateness_min=_decimal(lateness),
                missed_connection_passengers=missed,
            ),
            "costs": DomainCosts(
                ATC=_decimal(Fraction(delay, 1800)),
                PILOT=_decimal(used / 1600 + Fraction(missed, 40)),
                GROUND=_decimal(overtime / 40 + lateness / 60),
            ),
            "evidence_ids": [f"overlay.{section}" for section in DOMAINS] + ["policy.scoring"],
        }
    )


def _checks(results: list[constraints.ConstraintResult]) -> tuple[ConstraintResult, ...]:
    converted = []
    for result in results:
        # Operational paths resolve into the frozen section facts, not mutable files.
        evidence = []
        for reference in result.evidence_ids:
            parts = reference.split(".")
            if len(parts) >= 2 and parts[0] == "overlay" and parts[1] in DOMAINS:
                evidence.append(".".join(parts[:2]))
            elif reference in ("snapshot.versions", f"plan.{result.candidate_id}.plan_hash"):
                evidence.append("snapshot.versions")
            else:
                raise ServiceError("UNKNOWN_CHECK_EVIDENCE")
        evidence = sorted(set(evidence)) or ["overlay.gates"]
        converted.append(
            ConstraintResult.model_validate(
                {
                    **result.model_dump(mode="json"),
                    "check_id": f"{result.candidate_id}:{result.rule_id}",
                    "domain": None if result.domain == "SYSTEM" else result.domain,
                    "evidence_ids": evidence,
                }
            )
        )
    return tuple(converted)


def build_catalog(snapshot: ScenarioSnapshot) -> CatalogResult:
    definition = _definition(snapshot)
    missing = fallback.missing_critical_facts(definition)
    if definition.overlay.connections is None:
        missing.append("connections")
    if missing:
        return CatalogResult(candidates=(), shortlist_ids=(), blocking_reasons=tuple(missing))
    bounds = fallback.search_bounds(definition)
    if bounds.raw_candidate_count > 124:
        raise ServiceError("CATALOG_BUDGET_EXCEEDED")
    candidates = tuple(
        EvaluatedCandidate(plan=_plan(definition, item.plan), checks=_checks(item.checks))
        for item in fallback.evaluate_catalog(definition)
    )
    feasible = [
        item.plan for item in candidates if all(check.status == "PASS" for check in item.checks)
    ]
    shortlist: list[str] = []
    if feasible:
        best = [
            min(feasible, key=lambda p: (p.costs.for_role(role), p.candidate_id)) for role in Role
        ]
        best += [min(feasible, key=lambda p: p.joint_order()), min(feasible, key=_procedural_order)]
        for plan in best + sorted(feasible, key=lambda p: p.joint_order()):
            if plan.candidate_id not in shortlist and len(shortlist) < 6:
                shortlist.append(plan.candidate_id)
    return CatalogResult(candidates=candidates, shortlist_ids=tuple(shortlist))


def check_plan(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> tuple[ConstraintResult, ...]:
    definition = _definition(snapshot)
    try:
        native = constraints.derive_plan(definition, plan.landing_time_s, plan.taxi_route_id)
        expected = _plan(definition, native)
    except (KeyError, ValueError, ServiceError):
        # No partial check set can pass the coordinator's required-rule guard.
        return ()
    results = _checks(constraints.check_plan(definition, native))
    if expected != plan:
        results = tuple(
            check.model_copy(
                update={
                    "status": "FAIL",
                    "reason_code": "PLAN_NOT_CANONICAL",
                    "actual": plan.plan_hash,
                    "required": expected.plan_hash,
                    "operator": "canonical plan hash ==",
                    "units": "sha256",
                }
            )
            if check.rule_id == "SYSTEM-FRESH-01"
            else check
            for check in results
        )
    return results


def _procedural_order(plan: CandidatePlan) -> tuple[int, int, str, str, str]:
    return (
        plan.landing_time_s,
        plan.gate_in_time_s,
        plan.gate_id,
        plan.taxi_route_id,
        plan.candidate_id,
    )


def select_fallback(snapshot: ScenarioSnapshot, catalog: CatalogResult) -> FallbackResult:
    # Re-enumerate/check the full catalog as PR #1 requires; never trust caller's shortlist.
    result = fallback.select_fallback(_definition(snapshot), "BACKEND_FALLBACK")
    candidate_id = result.plan.candidate_id if result.plan else None
    if candidate_id and candidate_id not in {
        entry.plan.candidate_id for entry in catalog.candidates
    }:
        raise ServiceError("FALLBACK_OUTSIDE_CATALOG")
    return FallbackResult(
        candidate_id=candidate_id,
        outcome=result.status,
        reason_code="PROCEDURAL_SELECTION" if candidate_id else result.status,
    )
