"""Validate organizer inputs and reproducibly prepare the demo and frozen evaluation cases.

Run before starting the server: uv run python -m app.prepare.
Every operational assumption not supplied by the workbook is declared in the overlay.
"""

import argparse
import copy
import csv
import json
from pathlib import Path
from typing import Any

from app.operational_data import (
    FLIGHT_ID_FIELDS,
    WORKBOOK_NAME,
    file_hash,
    profile_flights,
    read_workbook,
)
from app.scenarios import ORGANIZER_RULESET, WorkbookPolicy, snapshot_from_dict

ROOT = Path(__file__).resolve().parent.parent

# Selection is fixed before model evaluation. Each case uses a distinct historical identity.
# Expected results express an independent outcome class, never the checker's own output.
CASES = (
    ("D01", "MIA", "weather", "tradeoff", "FEASIBLE"),
    ("D02", "HNL", "late", "fixed_gate", "FEASIBLE"),
    ("D03", "CLT", "both", "tradeoff", "FEASIBLE"),
    ("D04", "LGA", "any", "heavy", "FEASIBLE"),
    ("D05", "BOS", "any", "blocked_gates", "NO_PLAN"),
    ("D06", "SEA", "any", "missing_fuel", "INSUFFICIENT"),
    ("H01", "ORD", "weather", "fixed_gate", "FEASIBLE"),
    ("H02", "DEN", "late", "arrival_window", "FEASIBLE"),
    ("H03", "SFO", "both", "heavy", "FEASIBLE"),
    ("H04", "LAX", "later_month", "fixed_gate", "FEASIBLE"),
    ("H05", "ORD", "any", "notice_conflict", "FEASIBLE"),
    ("H06", "DEN", "any", "taxi_conflict", "FEASIBLE"),
    ("H07", "SFO", "any", "blocked_gates", "NO_PLAN"),
    ("H08", "LAX", "any", "impossible_fuel", "NO_PLAN"),
    ("H09", "ORD", "any", "missing_fuel", "INSUFFICIENT"),
    ("H10", "DEN", "any", "stale_approval", "STALE_BLOCKED"),
    ("H11", "SFO", "any", "timeout", "FALLBACK"),
    ("H12", "LAX", "any", "malformed", "FALLBACK"),
)


def matches(row: dict[str, str], airport: str, trigger: str) -> bool:
    weather, late = (
        float(row["WEATHER_DELAY"] or 0) > 15,
        float(row["LATE_AIRCRAFT_DELAY"] or 0) > 20,
    )
    return (
        row["DESTINATION_AIRPORT"] == airport
        and row["CANCELLED"] == row["DIVERTED"] == "0"
        and {
            "weather": weather and not late,
            "late": late and not weather,
            "both": weather and late,
            "any": weather or late,
            "later_month": int(row["MONTH"]) >= 6 and (weather or late),
        }[trigger]
    )


def definition(
    case_id: str,
    ordinal: int,
    row: dict[str, str],
    sha256: str,
    policy: WorkbookPolicy,
    variant: str,
) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((ROOT / "scenarios/D01.json").read_text())
    raw.update(
        scenario_id=case_id,
        scenario_revision=1,
        ruleset_version=ORGANIZER_RULESET,
        policy_version="balanced-workbook-v1",
        description=(
            f"Organizer workbook simulation: {variant}. "
            "Historical trigger only; operational state is synthetic."
        ),
    )
    raw["source"] = {
        "dataset_file": "flights.csv",
        "dataset_sha256": sha256,
        "record_ordinal": ordinal,
        "identity_fields": {key: row[key] for key in FLIGHT_ID_FIELDS},
    }
    o = raw["overlay"]
    o["search"]["earliest_landing_s"] = 1200
    o["runway"]["available_intervals"] = [[0, 86400]]
    o["movements"] = [
        {
            "movement_id": "BG1",
            "runway_id": "R1",
            "time_s": 900,
            "wake_category": "M",
            "occupancy_s": 60,
        },
        {
            "movement_id": "BG2",
            "runway_id": "R1",
            "time_s": 1380,
            "wake_category": "M",
            "occupancy_s": 60,
        },
    ]
    o["capacity"] = {"window_s": 3600, "max_movements": policy.max_ops_per_hour}
    o["separation_s"] = policy.separation_s
    o["gates"][0]["reservations"] = [[0, 1800]]
    o["gates"][1]["reservations"] = [[0, 1680]]
    o["crews"][0].update(
        regular_shift_end_s=5400, max_duty_end_s=7200, report_time_s=5400 - policy.ground_shift_s
    )
    o["crews"][1].update(
        regular_shift_end_s=1680, max_duty_end_s=7200, report_time_s=1680 - policy.ground_shift_s
    )
    burn = policy.narrowbody_hold_kg_per_hour / 60
    o["fuel"] = {
        "projected_fuel_at_earliest_kg": policy.spare_hold_min * burn,
        "hold_burn_kg_per_min": burn,
        "taxi_burn_kg_per_min": 20,
        "reserve_floor_kg": policy.reserve_hold_min * burn
        + 4000 * policy.contingency_percent / 100,
        "uncertainty_allowance_kg": 50,
    }
    o["service"].update(
        duration_s=policy.min_turnaround_s, target_service_end_s=1800 + policy.min_turnaround_s
    )
    o["connections"][0]["deadline_s"] = 5400
    o["operations"] = {
        "policy": policy.model_dump(mode="json"),
        "aircraft_kind": "narrowbody",
        "trip_fuel_kg": 4000,
        "flight_crew_report_s": -42000,
        "assigned_gate_id": "G1",
        "gate_decision_s": 0,
        "modeled_runway_ids": ["R1"],
        "wake_application": "departure_time_proxy_for_arrivals",
        "assumptions": [
            "Workbook values are organizer-supplied synthetic planning limits, "
            "not actual airport data.",
            "One runway and two candidate spare gates are a declared subset of the airport "
            "inventory. Other gates are unavailable to this decision.",
            "Background movements are a complete synthetic traffic schedule within the planning "
            "and capacity windows; filtered DOT rows are not a queue.",
            "M denotes Large; S/H/L denote Super/Heavy/Small. Same-runway departure time minima "
            "are an explicitly declared arrival simulation proxy; nautical-mile separation "
            "is not modeled.",
            "Narrowbody class, 4000 kg trip fuel, 20 kg/min taxi burn and 50 kg uncertainty "
            "allowance are synthetic inputs. Arrival spare fuel includes the reserve and "
            "contingency, a conservative interpretation.",
            "Flight duty ends at gate arrival. Report time and ground crew report times are "
            "synthetic; ground maximum duty ends at t=7200, an additional declared scenario cap.",
            "Gate-change notice is measured from t=0 to landing, conservatively before gate "
            "arrival. Routes, service targets, connections, reservations and equipment "
            "availability are synthetic.",
        ],
    }
    raw["provenance"]["operations"] = "synthetic_scenario"
    if variant == "fixed_gate":
        o["routes"] = o["routes"][:2]
    elif variant == "arrival_window":
        o["arrival_options"][0]["landing_window"] = [1260, 86400]
    elif variant == "heavy":
        o["movements"][1].update(time_s=1440, wake_category="H")
    elif variant == "notice_conflict":
        o["operations"]["gate_decision_s"] = 300
    elif variant == "taxi_conflict":
        o["taxi_blockages"] = {"TWY-C": [[1200, 2400]]}
    elif variant == "blocked_gates":
        for gate in o["gates"]:
            gate["reservations"] = [[0, 86400]]
    elif variant == "impossible_fuel":
        o["fuel"]["projected_fuel_at_earliest_kg"] = 1000
        o["operations"]["assumptions"].append(
            "Fault scenario: arrival fuel is 1000 kg instead of the workbook's default spare fuel."
        )
    elif variant == "missing_fuel":
        o["fuel"]["projected_fuel_at_earliest_kg"] = None
    snapshot_from_dict(raw)  # Reject malformed generated fixtures before writing them.
    return raw


def prepare(data_dir: Path, root: Path = ROOT) -> dict[str, Any]:
    profile = profile_flights(data_dir / "flights.csv")
    if profile["rows"] != 358774 or profile["invalid_year_or_filter"]:
        raise ValueError("Expected the organizer release: 358774 eligible 2015 records")
    selected: dict[str, tuple[int, dict[str, str]]] = {}
    used = set()
    with (data_dir / "flights.csv").open(newline="") as source:
        for ordinal, row in enumerate(csv.DictReader(source), 1):
            identity = tuple(row[key] for key in FLIGHT_ID_FIELDS)
            for case_id, airport, trigger, _, _ in CASES:
                if (
                    case_id not in selected
                    and identity not in used
                    and matches(row, airport, trigger)
                ):
                    selected[case_id] = (ordinal, row)
                    used.add(identity)
    if len(selected) != len(CASES):
        raise ValueError("Required scenario identities are absent from this release")
    policies = {
        airport: read_workbook(data_dir / WORKBOOK_NAME, airport)
        for airport in {c[1] for c in CASES}
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "dataset": profile,
        "workbook": {"file": WORKBOOK_NAME, "sha256": file_hash(data_dir / WORKBOOK_NAME)},
        "selection": (
            "Fixed airport/trigger first-match sampling; distinct flight identities "
            "and disjoint development/holdout airports. Synthetic templates share "
            "infrastructure; this suite is not a generalization benchmark."
        ),
        "cases": [],
    }
    for case_id, airport, trigger, variant, expected in CASES:
        ordinal, row = selected[case_id]
        raw = definition(case_id, ordinal, row, profile["sha256"], policies[airport], variant)
        split = "development" if case_id.startswith("D") else "holdout"
        relative = (
            Path("scenarios/organizer" if split == "development" else "evals/holdout")
            / f"{case_id}.json"
        )
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(raw, indent=2) + "\n")
        manifest["cases"].append(
            {
                "case_id": case_id,
                "split": split,
                "path": str(relative),
                "sha256": file_hash(target),
                "variant": variant,
                "expected": expected,
                "trigger": trigger,
                "identity": copy.deepcopy(raw["source"]["identity_fields"]),
            }
        )
    (root / "evals").mkdir(parents=True, exist_ok=True)
    (root / "evals/manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    manifest = prepare(args.data_dir)
    print(
        f"Verified {manifest['dataset']['rows']} rows; prepared "
        "6 development and 12 frozen holdout cases."
    )


if __name__ == "__main__":
    main()
