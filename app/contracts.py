"""Shared, versioned contracts for the API, agents, planner, and review hook.

The planner owns aviation arithmetic. This module validates structural integrity
and complete check coverage at the boundary with the coordinator and store.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[\w.:/-]+$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
Count = Annotated[StrictInt, Field(ge=0)]
Revision = Annotated[StrictInt, Field(ge=1)]
Nonnegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
IdList = Annotated[tuple[Identifier, ...], Field(max_length=124)]
ApiMode = Literal["responses", "chat_completions"]
REQUIRED_RULES = (
    "ATC-RWY-01",
    "ATC-SEP-01",
    "ATC-TAXI-01",
    "ATC-CAP-01",
    "PILOT-FUEL-01",
    "PILOT-ROUTE-01",
    "GROUND-GATE-01",
    "GROUND-OCC-01",
    "GROUND-CREW-01",
    "GROUND-EQUIP-01",
    "SYSTEM-FRESH-01",
)
SOURCE_ALLOWLIST = frozenset(
    {
        "YEAR",
        "MONTH",
        "DAY",
        "AIRLINE",
        "FLIGHT_NUMBER",
        "TAIL_NUMBER",
        "ORIGIN_AIRPORT",
        "DESTINATION_AIRPORT",
        "WEATHER_DELAY",
        "LATE_AIRCRAFT_DELAY",
        "CANCELLED",
        "DIVERTED",
        "SCHEDULED_DEPARTURE",
        "SCHEDULED_ARRIVAL",
    }
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Role(StrEnum):
    ATC = "ATC"
    PILOT = "PILOT"
    GROUND = "GROUND"


class Outcome(StrEnum):
    CONSENSUS = "FEASIBLE_CONSENSUS"
    FALLBACK = "FEASIBLE_FALLBACK"
    NO_PLAN = "NO_FEASIBLE_OPTION_IN_SEARCH_SPACE"
    INSUFFICIENT = "INSUFFICIENT_DATA"
    ERROR = "SYSTEM_ERROR"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class State(StrEnum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    PREPARING = "PREPARING"
    ROUND_1 = "ROUND_1"
    RECONCILING = "RECONCILING"
    ROUND_2 = "ROUND_2"
    FALLBACK = "FALLBACK"
    FINAL_VALIDATION = "FINAL_VALIDATION"
    TERMINAL = "TERMINAL"


class Approval(StrEnum):
    NONE = "NOT_APPLICABLE"
    PENDING = "PENDING"
    APPLIED = "APPLIED_SIMULATION"
    REJECTED = "REJECTED"
    STALE = "STALE"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class HistoricalRecord(Contract):
    dataset_id: Identifier
    dataset_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    record_ordinal: Revision
    identity: ShortText
    original_fields: dict[str, str | None]

    def prompt_seed(self) -> dict[str, str | None]:
        return {k: v for k, v in self.original_fields.items() if k in SOURCE_ALLOWLIST}

    def qualifies(self) -> bool:
        if any(self.original_fields.get(k) != "0" for k in ("CANCELLED", "DIVERTED")):
            return False
        for field, threshold in (("WEATHER_DELAY", 15), ("LATE_AIRCRAFT_DELAY", 20)):
            try:
                value = Decimal(self.original_fields.get(field) or "NaN")
                if value.is_finite() and value > threshold:
                    return True
            except InvalidOperation:
                continue
        return False


class Fact(Contract):
    fact_id: Identifier
    domain: Role | None
    value: JsonValue
    units: str
    provenance: Literal["historical_observed", "synthetic_scenario", "policy", "derived"]
    source_reference: ShortText
    source_version: Identifier
    observed_at: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    input_ids: IdList = ()


class DomainCosts(Contract):
    ATC: Nonnegative
    PILOT: Nonnegative
    GROUND: Nonnegative

    def for_role(self, role: Role) -> Decimal:
        return {Role.ATC: self.ATC, Role.PILOT: self.PILOT, Role.GROUND: self.GROUND}[role]


class ScenarioSnapshot(Contract):
    scenario_id: Identifier
    revision: Revision
    resource_id: Identifier
    resource_revision: Revision
    ruleset_version: Identifier
    policy_version: Identifier
    simulation_epoch: AwareDatetime
    source: HistoricalRecord
    facts: tuple[Fact, ...]
    required_fact_ids: Annotated[IdList, Field(min_length=1)]
    required_rule_ids: IdList = REQUIRED_RULES
    concession_delta: DomainCosts
    valid_until: AwareDatetime | None = None

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        ids = [f.fact_id for f in self.facts]
        if len(ids) != len(set(ids)):
            raise ValueError("Fact IDs must be unique")
        if len(self.required_rule_ids) != len(set(self.required_rule_ids)):
            raise ValueError("Rule IDs must be unique")
        if not set(REQUIRED_RULES).issubset(self.required_rule_ids):
            raise ValueError("The PRD's required hard rules cannot be removed")
        return self

    @property
    def snapshot_hash(self) -> str:
        return digest(self)

    def missing_inputs(self) -> tuple[str, ...]:
        facts = {f.fact_id: f for f in self.facts}
        return tuple(k for k in self.required_fact_ids if k not in facts or facts[k].value is None)

    def expired(self, now: datetime) -> bool:
        return (self.valid_until is not None and now >= self.valid_until) or any(
            f.valid_until is not None and now >= f.valid_until for f in self.facts
        )


class Interval(Contract):
    start_s: Count
    end_s: Count

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end_s < self.start_s:
            raise ValueError("Interval endpoints are reversed")
        return self


class RouteEdge(Interval):
    edge_id: Identifier


class ImpactVector(Contract):
    additional_landing_delay_s: Count
    modeled_fuel_used_kg: Nonnegative
    # Failed catalog candidates can project a deficit; required fuel checks block approval.
    fuel_at_gate_kg: Decimal
    reserve_margin_kg: Decimal
    crew_overtime_min: Nonnegative
    turnaround_lateness_min: Nonnegative
    missed_connection_passengers: Count | None


class CandidatePlan(Contract):
    candidate_id: Identifier
    scenario_id: Identifier
    scenario_revision: Revision
    resource_revision: Revision
    ruleset_version: Identifier
    policy_version: Identifier
    landing_time_s: Count
    runway_id: Identifier
    runway_occupancy_end_s: Count
    taxi_route_id: Identifier
    route_edge_intervals: tuple[RouteEdge, ...]
    gate_id: Identifier
    gate_in_time_s: Count
    service_start_s: Count
    service_end_s: Count
    gate_release_s: Count
    crew_id: Identifier
    arrival_option_id: Identifier
    impacts: ImpactVector
    costs: DomainCosts
    evidence_ids: IdList

    @model_validator(mode="after")
    def ordered(self) -> Self:
        times = [
            self.landing_time_s,
            self.gate_in_time_s,
            self.service_start_s,
            self.service_end_s,
            self.gate_release_s,
        ]
        if times != sorted(times) or self.runway_occupancy_end_s < self.landing_time_s:
            raise ValueError("Plan timestamps are inconsistent")
        return self

    @property
    def plan_hash(self) -> str:
        return digest(self)

    def joint_order(self) -> tuple[Decimal, Decimal, int, int, str, str, str]:
        values = [self.costs.for_role(role) for role in Role]
        return (
            max(values),
            sum(values, Decimal(0)),
            self.landing_time_s,
            self.gate_in_time_s,
            self.gate_id,
            self.taxi_route_id,
            self.candidate_id,
        )


class ConstraintResult(Contract):
    check_id: Identifier
    rule_id: Identifier
    rule_version: Identifier
    domain: Role | None
    candidate_id: Identifier
    status: Literal["PASS", "FAIL", "UNKNOWN"]
    actual: JsonValue
    operator: str
    required: JsonValue
    units: str
    margin: Decimal | None
    evidence_ids: Annotated[IdList, Field(min_length=1)]
    reason_code: Identifier


class EvaluatedCandidate(Contract):
    plan: CandidatePlan
    checks: tuple[ConstraintResult, ...]


class CatalogResult(Contract):
    candidates: Annotated[tuple[EvaluatedCandidate, ...], Field(max_length=124)]
    shortlist_ids: Annotated[tuple[Identifier, ...], Field(max_length=6)]
    blocking_reasons: tuple[ShortText, ...] = ()

    @model_validator(mode="after")
    def unique_candidates(self) -> Self:
        ids = [c.plan.candidate_id for c in self.candidates]
        if len(ids) != len(set(ids)) or len(self.shortlist_ids) != len(set(self.shortlist_ids)):
            raise ValueError("Duplicate candidate IDs")
        if not set(self.shortlist_ids).issubset(ids):
            raise ValueError("Shortlist references unknown candidates")
        return self

    @property
    def catalog_hash(self) -> str:
        return digest(self)


class FallbackResult(Contract):
    candidate_id: Identifier | None
    outcome: Literal[
        "FEASIBLE_FALLBACK",
        "NO_FEASIBLE_OPTION_IN_SEARCH_SPACE",
        "INSUFFICIENT_DATA",
        "SYSTEM_ERROR",
    ]
    reason_code: Identifier


class Objection(Contract):
    candidate_id: Identifier
    rule_id: Identifier
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=12)]
    reason_code: Identifier


class AgentOffer(Contract):
    preferred_candidate_id: Identifier | None
    ranked_candidate_ids: Annotated[tuple[Identifier, ...], Field(max_length=6)]
    acceptable_candidate_ids: Annotated[tuple[Identifier, ...], Field(max_length=6)]
    concession_candidate_ids: Annotated[tuple[Identifier, ...], Field(max_length=6)]
    objections: Annotated[tuple[Objection, ...], Field(max_length=6)]
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=12)]
    rationale: ShortText


class AgentVote(Contract):
    plan_hash: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    decision: Literal["ACCEPT", "REJECT", "NEEDS_INFORMATION"]
    reason_codes: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=6)]
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=12)]
    rationale: ShortText


class AgentMessage(Contract):
    schema_version: Literal["1"] = "1"
    message_id: Identifier
    run_id: Identifier
    sender_role: Role
    round: Literal[1, 2]
    message_type: Literal["OFFER", "VOTE"]
    scenario_revision: Revision
    resource_revision: Revision
    ruleset_version: Identifier
    policy_version: Identifier
    candidate_set_hash: str
    payload: AgentOffer | AgentVote
    received_at: AwareDatetime


class ProviderInfo(Contract):
    mode: Literal["LIVE", "FIXTURE", "UNAVAILABLE", "RECORDED_REPLAY"]
    model: str
    api_base: str | None = None
    api_mode: ApiMode = "responses"
    reasoning_effort: Literal["none"] | None = None
    prompt_version: str = "roles-v5"
    max_output_tokens: Annotated[StrictInt, Field(ge=1, le=500)] = 500
    retries: Literal[0] = 0


class RunRecord(Contract):
    run_id: Identifier
    parent_run_id: Identifier | None = None
    snapshot: ScenarioSnapshot
    planning_mode: Literal["INTEGRATED", "FIXTURE"]
    provider: ProviderInfo
    processing_state: State = State.CREATED
    negotiation_outcome: Outcome | None = None
    approval_state: Approval = Approval.NONE
    round_count: Annotated[StrictInt, Field(ge=0, le=2)] = 0
    agent_invocation_count: Annotated[StrictInt, Field(ge=0, le=6)] = 0
    model_call_count: Annotated[StrictInt, Field(ge=0, le=6)] = 0
    created_at: AwareDatetime
    deadline_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    elapsed_ms: Count | None = None
    catalog: CatalogResult | None = None
    candidate: CandidatePlan | None = None
    checks: tuple[ConstraintResult, ...] = ()
    offers: dict[Role, AgentOffer] = Field(default_factory=dict)
    votes: dict[Role, AgentVote] = Field(default_factory=dict)
    fallback_reason: str | None = None
    reason_code: str | None = None

    @property
    def plan_hash(self) -> str | None:
        return self.candidate.plan_hash if self.candidate else None


class AuditEvent(Contract):
    run_id: str
    sequence: int
    event_type: str
    actor: str
    occurred_at: AwareDatetime
    payload: dict[str, JsonValue]
    previous_hash: str
    event_hash: str


def verify_event_chain(events: list[AuditEvent]) -> bool:
    """Detect edits/reordering against the retained chain, not a trusted external anchor."""
    previous = ""
    for sequence, event in enumerate(events, start=1):
        if (
            event.sequence != sequence
            or event.previous_hash != previous
            or event.event_hash != digest(event.model_dump(mode="json", exclude={"event_hash"}))
            or event.run_id != events[0].run_id
        ):
            return False
        previous = event.event_hash
    return True


class ResourceState(Contract):
    resource_id: Identifier
    revision: Revision
    data: dict[str, JsonValue] = Field(default_factory=dict)


class StartRunRequest(Contract):
    scenario_id: Identifier
    scenario_revision: Revision
    parent_run_id: Identifier | None = None


class ReviewRequest(Contract):
    decision: Literal["APPROVE", "REJECT"]
    plan_hash: str
    scenario_revision: Revision
    resource_revision: Revision
    ruleset_version: Identifier
    policy_version: Identifier
    expires_at: AwareDatetime
    reason: ShortText
    acknowledge_fallback: StrictBool = False


class ReviewEffect(Contract):
    """Developer A returns checked resource changes from inside the transaction."""

    checks: tuple[ConstraintResult, ...]
    resource_data: dict[str, JsonValue] | None


@dataclass(frozen=True)
class PlanningHooks:
    """Three pure functions supplied by Developer C; no service/container layer."""

    build_catalog: Callable[[ScenarioSnapshot], CatalogResult]
    check_plan: Callable[[ScenarioSnapshot, CandidatePlan], tuple[ConstraintResult, ...]]
    select_fallback: Callable[[ScenarioSnapshot, CatalogResult], FallbackResult]
    mode: Literal["INTEGRATED", "FIXTURE"] = "INTEGRATED"


ReviewHook = Callable[[RunRecord, ResourceState, ReviewRequest], ReviewEffect]


class ServiceError(Exception):
    def __init__(self, code: str, status: int = 409) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def require_passing_checks(
    snapshot: ScenarioSnapshot,
    plan: CandidatePlan,
    checks: tuple[ConstraintResult, ...],
) -> None:
    """Prevent vacuous all([]), stale certificates, or missing domain coverage."""
    if (
        plan.scenario_id,
        plan.scenario_revision,
        plan.resource_revision,
        plan.ruleset_version,
        plan.policy_version,
    ) != (
        snapshot.scenario_id,
        snapshot.revision,
        snapshot.resource_revision,
        snapshot.ruleset_version,
        snapshot.policy_version,
    ):
        raise ServiceError("PLAN_VERSION_MISMATCH")
    rules = [c.rule_id for c in checks]
    check_ids = [c.check_id for c in checks]
    facts = {f.fact_id for f in snapshot.facts}
    if len(rules) != len(set(rules)) or len(check_ids) != len(set(check_ids)):
        raise ServiceError("DUPLICATE_CHECK")
    if not set(snapshot.required_rule_ids).issubset(rules):
        raise ServiceError("MISSING_REQUIRED_CHECK")
    if not set(plan.evidence_ids).issubset(facts):
        raise ServiceError("UNKNOWN_PLAN_EVIDENCE")
    for check in checks:
        if (
            check.candidate_id != plan.candidate_id
            or check.rule_version != snapshot.ruleset_version
        ):
            raise ServiceError("CHECK_VERSION_MISMATCH")
        if not set(check.evidence_ids).issubset(facts):
            raise ServiceError("UNKNOWN_CHECK_EVIDENCE")
        if check.rule_id in REQUIRED_RULES:
            prefix = check.rule_id.split("-")[0]
            expected_domain = Role(prefix) if prefix != "SYSTEM" else None
            if check.domain != expected_domain:
                raise ServiceError("CHECK_DOMAIN_MISMATCH")
        if check.rule_id in snapshot.required_rule_ids and check.status != "PASS":
            raise ServiceError("REQUIRED_CHECK_" + check.status)
