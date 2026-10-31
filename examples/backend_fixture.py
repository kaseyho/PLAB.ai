"""Small contract fixture for A/B integration while C builds the real planner.

Checks below are supplied test certificates. This is NOT an aviation rule engine,
an evaluation baseline, or evidence of operational feasibility.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from pydantic import JsonValue

from app.agents import ProviderReply
from app.contracts import (
    REQUIRED_RULES,
    AgentOffer,
    AgentVote,
    CandidatePlan,
    CatalogResult,
    ConstraintResult,
    DomainCosts,
    EvaluatedCandidate,
    Fact,
    FallbackResult,
    HistoricalRecord,
    ImpactVector,
    PlanningHooks,
    ProviderInfo,
    ResourceState,
    ReviewEffect,
    ReviewRequest,
    Role,
    RouteEdge,
    RunRecord,
    ScenarioSnapshot,
)


def sample_snapshot() -> ScenarioSnapshot:
    return ScenarioSnapshot(
        scenario_id="backend-fixture",
        revision=1,
        resource_id="fixture-airport",
        resource_revision=1,
        ruleset_version="toy-rules-v1",
        policy_version="balanced-v1",
        simulation_epoch=datetime(2026, 10, 31, tzinfo=UTC),
        source=HistoricalRecord(
            dataset_id="local-flights-2015",
            record_ordinal=71,
            identity="2015-01-01-AA1057-DFW-MIA",
            dataset_sha256="afa4e42188b113dd69427a3fbd00a4b6979c6235c866f963c2899d18a3403a83",
            original_fields={
                "YEAR": "2015",
                "MONTH": "1",
                "DAY": "1",
                "AIRLINE": "AA",
                "FLIGHT_NUMBER": "1057",
                "ORIGIN_AIRPORT": "DFW",
                "DESTINATION_AIRPORT": "MIA",
                "WEATHER_DELAY": "102",
                "LATE_AIRCRAFT_DELAY": "0",
                "CANCELLED": "0",
                "DIVERTED": "0",
            },
        ),
        facts=(
            Fact(
                fact_id="fixture-assumptions",
                domain=None,
                value="Supplied integration-test certificates; no operational rule engine",
                units="label",
                provenance="synthetic_scenario",
                source_reference="technical_prd.md Appendix A / backend integration fixture",
                source_version="fixture-v1",
            ),
        ),
        required_fact_ids=("fixture-assumptions",),
        concession_delta=DomainCosts(
            ATC=Decimal("0.25"), PILOT=Decimal("0.25"), GROUND=Decimal("0.25")
        ),
    )


def fixture_checks(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> tuple[ConstraintResult, ...]:
    checks = []
    for rule in REQUIRED_RULES:
        prefix = rule.split("-")[0]
        checks.append(
            ConstraintResult(
                check_id=f"{plan.candidate_id}:{rule}",
                rule_id=rule,
                rule_version=snapshot.ruleset_version,
                domain=Role(prefix) if prefix != "SYSTEM" else None,
                candidate_id=plan.candidate_id,
                status="PASS",
                actual="fixture certificate",
                operator="fixture",
                required="fixture certificate",
                units="fixture",
                margin=None,
                evidence_ids=("fixture-assumptions",),
                reason_code="FIXTURE_ONLY",
            )
        )
    return tuple(checks)


def fixture_catalog(snapshot: ScenarioSnapshot) -> CatalogResult:
    candidates = []
    for identifier, landing, taxi, fuel, margin, overtime, gate, atc, pilot, ground in (
        ("C1", 600, 480, "2840", "540", "20", "G2", "0", "0.1", "0.5"),
        ("C2", 960, 540, "2520", "220", "0", "G1", "0.2", "0.3", "0"),
        ("C3", 1200, 300, "2400", "100", "0", "G1", "0.333333333333", "0.375", "0"),
    ):
        gate_in = landing + taxi
        plan = CandidatePlan(
            candidate_id=identifier,
            scenario_id=snapshot.scenario_id,
            scenario_revision=snapshot.revision,
            resource_revision=snapshot.resource_revision,
            ruleset_version=snapshot.ruleset_version,
            policy_version=snapshot.policy_version,
            landing_time_s=landing,
            runway_id="R1",
            runway_occupancy_end_s=landing + 60,
            taxi_route_id=f"route-{identifier}",
            route_edge_intervals=(
                RouteEdge(edge_id=f"edge-{identifier}", start_s=landing, end_s=gate_in),
            ),
            gate_id=gate,
            gate_in_time_s=gate_in,
            service_start_s=gate_in,
            service_end_s=gate_in + 1200,
            gate_release_s=gate_in + 1200,
            crew_id=f"crew-{gate}",
            arrival_option_id="permitted-fixture",
            evidence_ids=("fixture-assumptions",),
            costs=DomainCosts(ATC=Decimal(atc), PILOT=Decimal(pilot), GROUND=Decimal(ground)),
            impacts=ImpactVector(
                additional_landing_delay_s=landing - 600,
                modeled_fuel_used_kg=Decimal(3000) - Decimal(fuel),
                fuel_at_gate_kg=Decimal(fuel),
                reserve_margin_kg=Decimal(margin),
                crew_overtime_min=Decimal(overtime),
                turnaround_lateness_min=Decimal(0),
                missed_connection_passengers=0,
            ),
        )
        candidates.append(EvaluatedCandidate(plan=plan, checks=fixture_checks(snapshot, plan)))
    return CatalogResult(candidates=tuple(candidates), shortlist_ids=("C1", "C2", "C3"))


def fixture_fallback(snapshot: ScenarioSnapshot, catalog: CatalogResult) -> FallbackResult:
    return FallbackResult(
        candidate_id="C1", outcome="FEASIBLE_FALLBACK", reason_code="FIXTURE_SELECTION"
    )


def fixture_planner() -> PlanningHooks:
    return PlanningHooks(fixture_catalog, fixture_checks, fixture_fallback, mode="FIXTURE")


def fixture_review(run: RunRecord, resource: ResourceState, request: ReviewRequest) -> ReviewEffect:
    assert run.candidate is not None
    return ReviewEffect(
        checks=fixture_checks(run.snapshot, run.candidate),
        resource_data={**resource.data, "fixture_applied_plan": run.candidate.plan_hash},
    )


class FixtureProvider:
    """Deterministic role outputs for protocol tests; model_call_count stays zero."""

    info = ProviderInfo(mode="FIXTURE", model="deterministic-test-double")

    async def generate(
        self,
        role: Role,
        round_number: int,
        prompt: dict[str, JsonValue],
        timeout_s: float,
    ) -> ProviderReply:
        context = cast(dict[str, JsonValue], prompt["context"])
        evidence = (f"impact:C2:{role}",)
        if round_number == 1:
            initial = cast(list[str], context["allowed_initial_ids"])
            concessions = cast(list[str], context["allowed_concession_ids"])
            offered = set(initial) | set(concessions)
            order = cast(list[str], context["domain_order"])
            result = AgentOffer(
                preferred_candidate_id=initial[0] if initial else None,
                ranked_candidate_ids=tuple(c for c in order if c in offered),
                acceptable_candidate_ids=tuple(initial),
                concession_candidate_ids=tuple(concessions),
                objections=(),
                evidence_ids=evidence,
                rationale="Fixture offer within the supplied envelope.",
            )
            return ProviderReply(result.model_dump_json())
        vote = AgentVote(
            plan_hash=cast(str, context["plan_hash"]),
            decision="ACCEPT",
            reason_codes=("FIXTURE_ACCEPT",),
            evidence_ids=evidence,
            rationale="Fixture acceptance of the exact proposed plan.",
        )
        return ProviderReply(vote.model_dump_json())

    async def close(self) -> None:
        pass
