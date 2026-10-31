import copy
import csv
import json
import shutil

import pytest
from openpyxl import load_workbook

from app import constraints, fallback
from app.contracts import ServiceError
from app.operational_data import WORKBOOK_NAME, file_hash, read_workbook, verify_source_files
from app.planning import build_catalog, joint_baseline, load_snapshot, snapshot_from_definition
from app.scenarios import snapshot_from_dict
from examples.backend_fixture import sample_snapshot
from tests.conftest import ROOT, result

BASE = json.loads((ROOT / "scenarios/organizer/D01.json").read_text())


def native(change=None):
    raw = copy.deepcopy(BASE)
    if change:
        change(raw["overlay"])
    return snapshot_from_dict(raw)


def checks(change=None, landing=1200, route="G2-route"):
    snapshot = native(change)
    return constraints.check_plan(snapshot, constraints.derive_plan(snapshot, landing, route))


def test_imported_workbook_values_and_cells_are_exact():
    policy = read_workbook(ROOT / "data" / WORKBOOK_NAME, "MIA")
    assert (policy.active_runways, policy.gates, policy.max_ops_per_hour) == (3, 70, 60)
    assert (policy.min_turnaround_s, policy.spare_gates) == (2100, 3)
    assert policy.source_cells["airport"] == "airport_capacity!A22:K22"
    assert (policy.separation_s["H>M"], policy.separation_s["M>M"], policy.separation_s["S>L"]) == (
        120,
        60,
        180,
    )
    assert policy.separation_nm["S>L"] == 8
    assert (
        policy.reserve_hold_min,
        policy.contingency_percent,
        policy.narrowbody_hold_kg_per_hour,
    ) == (30, 5, 2400)
    assert (
        policy.flight_crew_duty_s,
        policy.ground_shift_s,
        policy.overtime_multiplier,
        policy.gate_notice_s,
    ) == (46800, 28800, 1.5, 1200)


@pytest.mark.parametrize(
    "fault", ["missing_sheet", "units", "duplicate", "missing_value", "formula"]
)
def test_workbook_rejects_missing_ambiguous_or_uncomputed_cells(tmp_path, fault):
    path = tmp_path / WORKBOOK_NAME
    book = load_workbook(ROOT / "data" / WORKBOOK_NAME)
    if fault == "missing_sheet":
        del book["wake_separation"]
    elif fault == "units":
        book["wake_separation"]["D3"] = "minutes"
    elif fault == "duplicate":
        book["airport_capacity"]["A4"] = "MIA"
    elif fault == "missing_value":
        book["airport_capacity"]["I22"] = None
    else:
        book["airport_capacity"]["I22"] = "=60"
    book.save(path)
    book.close()
    with pytest.raises(ServiceError, match="WORKBOOK_SCHEMA_INVALID"):
        read_workbook(path, "MIA")


def test_independently_calculated_fuel_turnaround_and_overtime():
    snapshot = snapshot_from_definition(native(), sample_snapshot().source)
    catalog = build_catalog(snapshot)
    baseline = next(e for e in catalog.candidates if e.plan.candidate_id == "L1200-G2-route")
    assert all(c.status == "PASS" for c in baseline.checks)
    assert len(baseline.checks) == 15
    assert baseline.plan.impacts.fuel_at_gate_kg == 1640  # 45*40 - 8*20
    assert baseline.plan.impacts.reserve_margin_kg == 190  # 1640 - 30*40 - .05*4000 - 50
    assert baseline.plan.service_end_s - baseline.plan.service_start_s == 2100
    assert baseline.plan.impacts.crew_overtime_min == 35
    assert str(baseline.plan.costs.GROUND) == "1.3125"  # 35 * 1.5 / 40
    assert joint_baseline(catalog).plan.candidate_id == "L1260-G1-long"
    assert len(catalog.candidates) == 93


@pytest.mark.parametrize("delta,status", [(0, "PASS"), (1, "FAIL")])
def test_gate_notice_and_flight_duty_exact_boundaries(delta, status):
    notice = checks(lambda o: o["operations"].update(gate_decision_s=delta))
    assert result(notice, "GROUND-NOTICE-01").status == status
    duty = checks(lambda o: o["operations"].update(flight_crew_report_s=1680 - 46800 - delta))
    assert result(duty, "PILOT-DUTY-01").status == status
    turn = checks(lambda o: o["service"].update(duration_s=2100 - delta))
    assert result(turn, "GROUND-TURN-01").status == status
    fuel = checks(lambda o: o["fuel"].update(projected_fuel_at_earliest_kg=1610 - delta))
    assert result(fuel, "PILOT-FUEL-01").status == status


def test_unchanged_gate_does_not_require_new_notice():
    found = checks(
        lambda o: o["operations"].update(gate_decision_s=1200), landing=1260, route="G1-long"
    )
    assert result(found, "GROUND-NOTICE-01").status == "PASS"


def test_widebody_hour_to_minute_conversion():
    def change(o):
        o["operations"]["aircraft_kind"] = "widebody"
        o["fuel"].update(
            hold_burn_kg_per_min=6500 / 60,
            reserve_floor_kg=3450,
            projected_fuel_at_earliest_kg=4875,
        )

    assert result(checks(change), "SYSTEM-OPS-01").status == "PASS"


def test_capacity_counts_other_runways_but_wake_and_occupancy_do_not():
    def change(o):
        o["operations"]["modeled_runway_ids"] = ["R1", "R2"]
        o["movements"] = [
            {
                "movement_id": f"M{i}",
                "time_s": 1200 + i,
                "runway_id": "R2",
                "wake_category": "H",
                "occupancy_s": 1,
            }
            for i in range(60)
        ]

    found = checks(change)
    assert result(found, "ATC-CAP-01").status == "FAIL"
    assert result(found, "ATC-CAP-01").actual["movements"] == 61
    assert result(found, "ATC-RWY-01").status == "PASS"
    assert result(found, "ATC-SEP-01").status == "PASS"


@pytest.mark.parametrize("field", ["trip_fuel_kg", "flight_crew_report_s"])
def test_missing_operational_facts_stop_before_enumeration(field):
    snapshot = native(lambda o: o["operations"].update({field: None}))
    answer = fallback.select_fallback(snapshot, "TEST")
    assert answer.status == "INSUFFICIENT_DATA"
    assert answer.evaluated_count == 0


def test_inventory_and_ground_shift_cannot_expand_beyond_workbook():
    inventory = checks(
        lambda o: o["operations"].update(modeled_runway_ids=["R1", "R2", "R3", "R4"])
    )
    assert result(inventory, "SYSTEM-OPS-01").status == "FAIL"
    shift = checks(lambda o: o["crews"][0].update(report_time_s=0))
    assert result(shift, "SYSTEM-OPS-01").status == "FAIL"


def bound_source(tmp_path):
    raw = copy.deepcopy(BASE)
    row = {**sample_snapshot().source.original_fields, **raw["source"]["identity_fields"]}
    with (tmp_path / "flights.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, list(row))
        writer.writeheader()
        writer.writerow(row)
    shutil.copy(ROOT / "data" / WORKBOOK_NAME, tmp_path / WORKBOOK_NAME)
    raw["source"].update(record_ordinal=1, dataset_sha256=file_hash(tmp_path / "flights.csv"))
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(raw))
    return path, raw


def test_source_identity_hash_and_workbook_binding(tmp_path):
    path, raw = bound_source(tmp_path)
    snapshot = load_snapshot(path, tmp_path)
    verify_source_files(snapshot, tmp_path)
    raw["source"]["identity_fields"]["FLIGHT_NUMBER"] = "WRONG"
    path.write_text(json.dumps(raw))
    with pytest.raises(ServiceError, match="SOURCE_RECORD_IDENTITY_MISMATCH"):
        load_snapshot(path, tmp_path)
    with (tmp_path / "flights.csv").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ServiceError, match="SOURCE_FILE_HASH_MISMATCH"):
        load_snapshot(path, tmp_path)
    with pytest.raises(ServiceError, match="SOURCE_FILE_CHANGED"):
        verify_source_files(snapshot, tmp_path)


def test_missing_or_changed_workbook_never_uses_legacy_defaults(tmp_path):
    path, raw = bound_source(tmp_path)
    raw["overlay"]["operations"]["policy"]["gates"] = 999
    path.write_text(json.dumps(raw))
    with pytest.raises(ServiceError, match="WORKBOOK_VERSION_OR_POLICY_MISMATCH"):
        load_snapshot(path, tmp_path)
    (tmp_path / WORKBOOK_NAME).unlink()
    with pytest.raises(ServiceError, match="WORKBOOK_FILE_NOT_FOUND"):
        load_snapshot(path, tmp_path)
