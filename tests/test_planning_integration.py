"""Prove PR #1's real checker runs through the backend contracts and API."""

import csv
import hashlib
import json
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import planning
from app.agents import UnavailableProvider
from app.contracts import (
    Approval,
    Outcome,
    PlanningHooks,
    ResourceState,
    ServiceError,
    StartRunRequest,
    require_passing_checks,
)
from app.coordinator import Coordinator
from app.main import Settings, create_app
from app.store import Store
from examples.backend_fixture import FixtureProvider, sample_snapshot
from tests.conftest import D01, scenario


def shared(mutate=None):
    return planning.snapshot_from_definition(scenario(mutate), sample_snapshot().source)


def hooks():
    return PlanningHooks(planning.build_catalog, planning.check_plan, planning.select_fallback)


def test_catalog_costs_shortlist_and_fallback_match_prd_worked_example():
    snapshot = shared()
    catalog = planning.build_catalog(snapshot)
    assert len(catalog.candidates) == 93
    assert sum(all(c.status == "PASS" for c in entry.checks) for entry in catalog.candidates) == 14
    assert len(catalog.shortlist_ids) <= 6
    feasible = []
    for entry in catalog.candidates:
        if entry.plan.candidate_id in catalog.shortlist_ids:
            require_passing_checks(snapshot, entry.plan, entry.checks)
            feasible.append(entry.plan)
    compromise = next(p for p in feasible if p.candidate_id == "L960-G1-long")
    assert compromise.costs.ATC == Decimal("0.2")
    assert compromise.costs.PILOT == Decimal("0.3")
    assert compromise.costs.GROUND == 0
    assert compromise.impacts.fuel_at_gate_kg == 2520
    assert compromise.impacts.reserve_margin_kg == 220
    fallback = planning.select_fallback(snapshot, catalog)
    assert fallback.candidate_id == "L600-G2-route"


@pytest.mark.parametrize(
    "field,value",
    [
        ("gate_in_time_s", 1501),
        ("gate_id", "G2"),
        ("arrival_option_id", "INVENTED"),
        ("resource_revision", 2),
    ],
)
def test_shared_candidate_tampering_is_rechecked_and_blocked(field, value):
    snapshot = shared()
    plan = next(
        e.plan
        for e in planning.build_catalog(snapshot).candidates
        if e.plan.candidate_id == "L960-G1-long"
    )
    tampered = plan.model_copy(update={field: value})
    checks = planning.check_plan(snapshot, tampered)
    assert any(c.rule_id == "SYSTEM-FRESH-01" and c.status == "FAIL" for c in checks)
    with pytest.raises(ServiceError):
        require_passing_checks(snapshot, tampered, checks)


def test_model_supplied_impact_and_cost_edits_do_not_pass_revalidation():
    snapshot = shared()
    plan = planning.build_catalog(snapshot).candidates[0].plan
    for changes in (
        {"impacts": plan.impacts.model_copy(update={"fuel_at_gate_kg": Decimal(9000)})},
        {"costs": plan.costs.model_copy(update={"PILOT": Decimal(0)})},
    ):
        assert any(
            c.status == "FAIL"
            for c in planning.check_plan(snapshot, plan.model_copy(update=changes))
        )


@pytest.mark.parametrize("shift_end,overtime", [(3000, 0), (2100, 10), (1400, 20)])
def test_crew_overtime_counts_only_task_time_after_the_shift(shift_end, overtime):
    snapshot = shared(lambda raw: raw["overlay"]["crews"][0].update(regular_shift_end_s=shift_end))
    plan = next(
        e.plan
        for e in planning.build_catalog(snapshot).candidates
        if e.plan.candidate_id == "L960-G1-long"
    )
    # Service is [1500, 2700): waiting before the task is never charged as overtime.
    assert plan.impacts.crew_overtime_min == overtime
    assert plan.costs.GROUND == Decimal(overtime) / 40


@pytest.mark.parametrize("fuel,expected", [(None, Outcome.INSUFFICIENT), (0, Outcome.NO_PLAN)])
async def test_missing_or_impossible_fuel_never_invokes_agents_or_creates_plan(
    tmp_path, fuel, expected
):
    snapshot = shared(lambda raw: raw["overlay"]["fuel"].update(projected_fuel_at_earliest_kg=fuel))
    store = Store(tmp_path / "run.sqlite3")
    store.register_scenario(snapshot, ResourceState(resource_id=snapshot.resource_id, revision=1))
    provider = FixtureProvider()
    run, _ = store.create_run(
        StartRunRequest(scenario_id="D01", scenario_revision=1),
        "start",
        provider.info,
        "INTEGRATED",
        45,
    )
    result = await Coordinator(store, hooks(), provider).run(run.run_id)
    assert result.negotiation_outcome == expected
    assert result.agent_invocation_count == 0
    assert result.candidate is None and result.approval_state == Approval.NONE


def test_source_loader_preserves_actual_row_and_hash(tmp_path):
    rows = [sample_snapshot().source.original_fields] * 71
    data = tmp_path / "data"
    data.mkdir()
    csv_path = data / "flights.csv"
    with csv_path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    path = tmp_path / "D01.json"
    path.write_text(json.dumps(D01))
    snapshot = planning.load_snapshot(path, data)
    assert snapshot.source.original_fields == rows[70]
    assert snapshot.source.dataset_sha256 == hashlib.sha256(csv_path.read_bytes()).hexdigest()
    assert snapshot.source.qualifies()


def test_oversized_catalog_is_rejected_before_allocation():
    snapshot = shared(lambda raw: raw["overlay"]["search"].update(horizon_s=100_000_000))
    with pytest.raises(ValueError, match="124"):
        planning.build_catalog(snapshot)


def test_fuel_reserve_cannot_be_lowered_by_editing_a_synthetic_fact():
    snapshot = shared()
    facts = tuple(
        f.model_copy(update={"value": {**f.value, "reserve_floor_kg": 0}})
        if f.fact_id == "overlay.fuel"
        else f
        for f in snapshot.facts
    )
    with pytest.raises(ServiceError, match="FUEL_POLICY_MISMATCH"):
        planning.build_catalog(snapshot.model_copy(update={"facts": facts}))


def test_revised_snapshot_requires_current_version_evidence():
    snapshot = shared().model_copy(update={"revision": 2, "resource_revision": 2})
    with pytest.raises(ServiceError, match="VERSION_EVIDENCE_MISMATCH"):
        planning.build_catalog(snapshot)
    facts = tuple(
        f.model_copy(update={"value": {**f.value, "scenario_revision": 2, "resource_revision": 2}})
        if f.fact_id == "snapshot.versions"
        else f
        for f in snapshot.facts
    )
    current = snapshot.model_copy(update={"facts": facts})
    catalog = planning.build_catalog(current)
    candidate_id = planning.select_fallback(current, catalog).candidate_id
    plan = next(e.plan for e in catalog.candidates if e.plan.candidate_id == candidate_id)
    require_passing_checks(current, plan, planning.check_plan(current, plan))
    assert plan.scenario_revision == plan.resource_revision == 2


def test_unknown_checker_evidence_cannot_be_substituted(monkeypatch):
    original = planning.constraints.check_plan

    def invalid_evidence(snapshot, plan):
        checks = original(snapshot, plan)
        checks[0] = checks[0].model_copy(update={"evidence_ids": ["invented.fact"]})
        return checks

    monkeypatch.setattr(planning.fallback, "check_plan", invalid_evidence)
    with pytest.raises(ServiceError, match="UNKNOWN_CHECK_EVIDENCE"):
        planning.build_catalog(shared())


def test_unknown_reference_outcomes_do_not_change_planning():
    snapshot = shared()
    original = planning.build_catalog(snapshot)
    source = snapshot.source.model_copy(
        update={
            "original_fields": {
                **snapshot.source.original_fields,
                "ARRIVAL_DELAY": "999",
                "AIR_TIME": "1",
            }
        }
    )
    assert planning.build_catalog(snapshot.model_copy(update={"source": source})) == original


def test_api_discovers_real_planner_and_returns_persisted_fallback(tmp_path):
    settings = Settings(
        db_path=tmp_path / "api.sqlite3",
        scenario_dir=tmp_path / "none",
        session_token="local-test-session-with-16-characters",
    )
    store = Store(settings.db_path)
    snapshot = shared()
    store.register_scenario(snapshot, ResourceState(resource_id=snapshot.resource_id, revision=1))
    with TestClient(
        create_app(settings=settings, store=store, provider=UnavailableProvider())
    ) as client:
        assert client.get("/health").json()["planning_connected"] is True
        response = client.post(
            "/api/runs",
            json={"scenario_id": "D01", "scenario_revision": 1},
            headers={
                "Authorization": "Bearer " + settings.session_token,
                "Idempotency-Key": "start",
            },
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        for _ in range(200):
            result = client.get(f"/api/runs/{run_id}").json()
            if result["processing_state"] == "TERMINAL":
                break
            time.sleep(0.01)
        assert result["negotiation_outcome"] == "FEASIBLE_FALLBACK"
        assert result["planning_mode"] == "INTEGRATED"
        assert result["candidate"]["candidate_id"] == "L600-G2-route"
        assert result["model_call_count"] == 0
        assert all(
            c["status"] == "PASS" and c["reason_code"] != "FIXTURE_ONLY" for c in result["checks"]
        )
        assert store.get_resource(snapshot.resource_id).revision == 1


def test_missing_dataset_is_visible_without_fabricating_a_source_row(tmp_path):
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    (scenarios_dir / "D01.json").write_text(json.dumps(D01))
    settings = Settings(
        db_path=tmp_path / "missing.sqlite3",
        scenario_dir=scenarios_dir,
        data_dir=tmp_path / "absent",
    )
    with TestClient(create_app(settings=settings, provider=UnavailableProvider())) as client:
        health = client.get("/health").json()
        assert health["status"] == "degraded"
        assert health["scenario_load_errors"] == [
            {"file": "D01.json", "code": "SOURCE_FILE_NOT_FOUND"}
        ]
        assert client.get("/api/scenarios").json() == []
