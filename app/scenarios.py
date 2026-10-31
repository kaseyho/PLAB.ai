"""Typed operational facts (PRD 5.5) loaded from a labelled synthetic overlay.

Every operational number comes from the overlay file, never from an agent. Required facts
may be left null in an overlay; checks then report UNKNOWN and fallback reports
INSUFFICIENT_DATA instead of guessing. Values that are present but invalid (negative,
unordered intervals, dangling references) are rejected at load.

Historical row extraction belongs to the data owner; the snapshot only keeps its locator.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = ROOT / "scenarios"

ProvenanceClass = Literal["historical_observed", "synthetic_scenario", "policy", "derived"]


class ScenarioValidationError(ValueError):
    """Overlay or policy is malformed; reject before any planning (maps to INPUT_REJECTED)."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


def _check_interval(interval: tuple[int, int]) -> tuple[int, int]:
    start, end = interval
    if start < 0 or end <= start:
        raise ValueError(f"interval must satisfy 0 <= start < end, got {interval}")
    return interval


class Movement(_Frozen):
    movement_id: str
    time_s: int = Field(ge=0)
    wake_category: str | None
    occupancy_s: int = Field(gt=0)


class Runway(_Frozen):
    runway_id: str
    available_intervals: list[tuple[int, int]]
    focal_occupancy_s: int | None = Field(default=None, gt=0)

    @field_validator("available_intervals")
    @classmethod
    def _intervals(cls, value: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return [_check_interval(i) for i in value]


class Capacity(_Frozen):
    window_s: int = Field(gt=0)
    max_movements: int = Field(gt=0)


class RouteEdge(_Frozen):
    edge_id: str
    duration_s: int = Field(gt=0)


class Route(_Frozen):
    route_id: str
    gate_id: str
    edges: list[RouteEdge] = Field(min_length=1)

    @property
    def taxi_duration_s(self) -> int:
        return sum(edge.duration_s for edge in self.edges)


class Gate(_Frozen):
    gate_id: str
    compatible_classes: list[str]
    reservations: list[tuple[int, int]]
    crew_id: str
    required_equipment: list[str]

    @field_validator("reservations")
    @classmethod
    def _intervals(cls, value: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return [_check_interval(i) for i in value]


class Crew(_Frozen):
    crew_id: str
    available_from_s: int = Field(ge=0)
    regular_shift_end_s: int = Field(ge=0)
    max_duty_end_s: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.available_from_s <= self.regular_shift_end_s <= self.max_duty_end_s:
            raise ValueError(
                "crew times must satisfy available_from <= regular_shift_end <= max_duty_end"
            )
        return self


class Equipment(_Frozen):
    equipment_id: str
    available_intervals: list[tuple[int, int]]

    @field_validator("available_intervals")
    @classmethod
    def _intervals(cls, value: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return [_check_interval(i) for i in value]


class Fuel(_Frozen):
    projected_fuel_at_earliest_kg: float | None = Field(default=None, ge=0)
    hold_burn_kg_per_min: float | None = Field(default=None, ge=0)
    taxi_burn_kg_per_min: float | None = Field(default=None, ge=0)
    reserve_floor_kg: float | None = Field(default=None, ge=0)
    uncertainty_allowance_kg: float | None = Field(default=None, ge=0)


class ArrivalOption(_Frozen):
    """Predeclared weather/route permission for landings inside [start, end). Null means unknown."""

    arrival_option_id: str
    landing_window: tuple[int, int]
    permitted: bool | None

    @field_validator("landing_window")
    @classmethod
    def _interval(cls, value: tuple[int, int]) -> tuple[int, int]:
        return _check_interval(value)


class Service(_Frozen):
    duration_s: int = Field(gt=0)
    pre_service_buffer_s: int = Field(ge=0)
    release_buffer_s: int = Field(ge=0)
    gate_buffer_s: int = Field(ge=0)
    target_service_end_s: int = Field(ge=0)


class ConnectionGroup(_Frozen):
    group_id: str
    passengers: int = Field(ge=0)
    transfer_time_s: int = Field(ge=0)
    deadline_s: int = Field(ge=0)


class Search(_Frozen):
    earliest_landing_s: int = Field(ge=0)
    horizon_s: int = Field(gt=0)
    step_s: int = Field(gt=0)


class Aircraft(_Frozen):
    aircraft_class: str | None
    wake_category: str | None


class Overlay(_Frozen):
    """The synthetic operational facts the historical CSV does not contain."""

    search: Search
    aircraft: Aircraft
    runway: Runway
    movements: list[Movement]
    separation_s: dict[str, int]  # key "LEADER>FOLLOWER", illustrative scenario policy
    capacity: Capacity
    routes: list[Route]
    taxi_blockages: dict[str, list[tuple[int, int]]]  # edge_id -> blocked intervals
    gates: list[Gate]
    crews: list[Crew]
    equipment: list[Equipment]
    fuel: Fuel
    arrival_options: list[ArrivalOption]
    service: Service
    connections: list[ConnectionGroup] | None

    @field_validator("separation_s")
    @classmethod
    def _separation(cls, value: dict[str, int]) -> dict[str, int]:
        for key, seconds in value.items():
            if key.count(">") != 1 or seconds < 0:
                raise ValueError(
                    f"separation requires 'LEADER>FOLLOWER' and seconds >= 0: {key}: {seconds}"
                )
        return value

    @field_validator("taxi_blockages")
    @classmethod
    def _blockages(
        cls, value: dict[str, list[tuple[int, int]]]
    ) -> dict[str, list[tuple[int, int]]]:
        return {edge: [_check_interval(i) for i in intervals] for edge, intervals in value.items()}

    @model_validator(mode="after")
    def _references(self) -> Self:
        gate_ids = {gate.gate_id for gate in self.gates}
        crew_ids = {crew.crew_id for crew in self.crews}
        equipment_ids = {item.equipment_id for item in self.equipment}
        for route in self.routes:
            if route.gate_id not in gate_ids:
                raise ValueError(f"route {route.route_id} ends at unknown gate {route.gate_id}")
        for gate in self.gates:
            if gate.crew_id not in crew_ids:
                raise ValueError(f"gate {gate.gate_id} assigns unknown crew {gate.crew_id}")
            unknown = set(gate.required_equipment) - equipment_ids
            if unknown:
                raise ValueError(
                    f"gate {gate.gate_id} requires unknown equipment {sorted(unknown)}"
                )
        return self

    def gate(self, gate_id: str) -> Gate:
        return next(gate for gate in self.gates if gate.gate_id == gate_id)

    def crew(self, crew_id: str) -> Crew:
        return next(crew for crew in self.crews if crew.crew_id == crew_id)

    def route(self, route_id: str) -> Route:
        return next(route for route in self.routes if route.route_id == route_id)


class SourceLocator(_Frozen):
    """Pointer to the historical row; the loader verifies source data separately."""

    dataset_file: str
    record_ordinal: int = Field(gt=0)


class ScenarioSnapshot(_Frozen):
    scenario_id: str
    scenario_revision: int
    resource_revision: int
    ruleset_version: str
    policy_version: str
    simulation_epoch_utc: str
    description: str
    source: SourceLocator
    overlay: Overlay
    provenance: dict[str, ProvenanceClass]
    snapshot_hash: str


RULESET_VERSION = "toy-rules-v1"


def canonical_hash(payload: object) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def snapshot_from_dict(raw: dict[str, Any]) -> ScenarioSnapshot:
    """Validate an overlay document and freeze it with a content hash."""
    try:
        body = {
            "scenario_id": raw["scenario_id"],
            "scenario_revision": raw["scenario_revision"],
            "resource_revision": raw["resource_revision"],
            "ruleset_version": raw["ruleset_version"],
            "policy_version": raw["policy_version"],
            "simulation_epoch_utc": raw["simulation_epoch_utc"],
            "description": raw["description"],
            "source": SourceLocator.model_validate(raw["source"]),
            "overlay": Overlay.model_validate(raw["overlay"]),
            "provenance": raw["provenance"],
        }
    except (KeyError, ValueError) as error:
        raise ScenarioValidationError(f"scenario {raw.get('scenario_id')}: {error}") from error
    if body["ruleset_version"] != RULESET_VERSION:
        raise ScenarioValidationError(f"unsupported ruleset {body['ruleset_version']}")
    missing_provenance = set(Overlay.model_fields) - set(body["provenance"])
    if missing_provenance:
        raise ScenarioValidationError(
            f"overlay sections without provenance: {sorted(missing_provenance)}"
        )
    hashed = {
        key: value.model_dump() if isinstance(value, BaseModel) else value
        for key, value in body.items()
    }
    try:
        return ScenarioSnapshot(**body, snapshot_hash=canonical_hash(hashed))
    except ValueError as error:
        raise ScenarioValidationError(str(error)) from error


def load_scenario(
    scenario_id: str, revision: int, scenario_dir: Path = SCENARIO_DIR
) -> ScenarioSnapshot:
    path = scenario_dir / f"{scenario_id}.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ScenarioValidationError(f"scenario {scenario_id}: {error}") from error
    if raw.get("scenario_revision") != revision:
        raise ScenarioValidationError(
            f"scenario {scenario_id}: revision {raw.get('scenario_revision')}, requested {revision}"
        )
    return snapshot_from_dict(raw)
