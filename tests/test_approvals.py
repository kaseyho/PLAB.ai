"""Developer A's review hook through the real Store and API, on the D01 worked example."""

import asyncio
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import planning
from app.approvals import APPLIED_KEY, current_snapshot, review_plan
from app.contracts import (
    Approval,
    Outcome,
    PlanningHooks,
    ResourceState,
    ReviewRequest,
    ServiceError,
    StartRunRequest,
    canonical_json,
)
from app.coordinator import Coordinator
from app.main import Settings, create_app
from app.store import Store
from examples.backend_fixture import FixtureProvider, sample_snapshot
from tests.conftest import scenario

TOKEN = "test-session-with-at-least-16-characters"
AUTH = {"Authorization": "Bearer " + TOKEN}
HOOKS = PlanningHooks(planning.build_catalog, planning.check_plan, planning.select_fallback)


def d01():
    return planning.snapshot_from_definition(scenario(), sample_snapshot().source)


@pytest.fixture
def d01_store(tmp_path):
    store = Store(tmp_path / "review.sqlite3")
    snapshot = d01()
    store.register_scenario(snapshot, ResourceState(resource_id=snapshot.resource_id, revision=1))
    return store


def pending_run(store, key="start"):
    provider = FixtureProvider()
    run, _ = store.create_run(
        StartRunRequest(scenario_id="D01", scenario_revision=1),
        key,
        provider.info,
        "INTEGRATED",
        45,
    )
    result = asyncio.run(Coordinator(store, HOOKS, provider).run(run.run_id))
    assert (result.negotiation_outcome, result.approval_state) == (
        Outcome.CONSENSUS,
        Approval.PENDING,
    )
    assert result.candidate.candidate_id == "L960-G1-long"
    return result


def approve(run, decision="APPROVE", **extra):
    return ReviewRequest(
        decision=decision,
        plan_hash=run.plan_hash,
        scenario_revision=run.snapshot.revision,
        resource_revision=run.snapshot.resource_revision,
        ruleset_version=run.snapshot.ruleset_version,
        policy_version=run.snapshot.policy_version,
        expires_at=run.expires_at,
        reason=extra.get("reason", "Checked margins and evidence"),
    )


def resource(store):
    with store.transaction() as db:
        return store._resource(db, "airport:MIA")


def test_approval_rechecks_and_books_the_runway_slot_and_gate(d01_store):
    run = pending_run(d01_store)
    applied = d01_store.review(run.run_id, approve(run), "key-1", "duty-manager", review_plan)
    assert applied.approval_state == Approval.APPLIED
    assert {c.status for c in applied.checks} == {"PASS"}
    state = resource(d01_store)
    assert state.revision == 2
    [booking] = state.data[APPLIED_KEY]
    # C2: lands 960 s, 60 s runway occupancy, G1 from gate-in 1500 s to release 2700 s
    assert (booking["landing_time_s"], booking["runway_occupancy_s"]) == (960, 60)
    assert (booking["gate_id"], booking["gate_interval_s"]) == ("G1", [1500, 2700])
    assert booking["plan_hash"] == run.plan_hash


def test_duplicate_key_applies_once_and_second_key_is_refused(d01_store):
    run = pending_run(d01_store)
    first = d01_store.review(run.run_id, approve(run), "key-1", "duty-manager", review_plan)
    again = d01_store.review(run.run_id, approve(run), "key-1", "duty-manager", review_plan)
    assert again == first
    with pytest.raises(ServiceError, match="REVIEW_NOT_PENDING"):
        d01_store.review(run.run_id, approve(run), "key-2", "duty-manager", review_plan)
    assert resource(d01_store).revision == 2
    assert len(resource(d01_store).data[APPLIED_KEY]) == 1


def test_reject_leaves_the_simulated_airport_unchanged(d01_store):
    run = pending_run(d01_store)
    rejected = d01_store.review(
        run.run_id,
        approve(run, "REJECT", reason="Prefer to wait"),
        "key-r",
        "duty-manager",
        review_plan,
    )
    assert rejected.approval_state == Approval.REJECTED
    assert resource(d01_store) == ResourceState(resource_id="airport:MIA", revision=1)


def test_booking_made_since_the_plan_blocks_approval_and_rolls_back(d01_store):
    # Someone booked G1 and the 960 s slot without bumping the revision; the recheck still sees it.
    run = pending_run(d01_store)
    clash = {
        APPLIED_KEY: [
            {
                "booking_id": "applied:other",
                "run_id": "other",
                "plan_hash": "x",
                "candidate_id": "x",
                "runway_id": "R1",
                "landing_time_s": 960,
                "runway_occupancy_s": 60,
                "wake_category": "M",
                "gate_id": "G1",
                "gate_interval_s": [1500, 2700],
            }
        ]
    }
    with sqlite3.connect(d01_store.path) as db:
        db.execute(
            "UPDATE resource_states SET data=? WHERE resource_id=?",
            (
                canonical_json(ResourceState(resource_id="airport:MIA", revision=1, data=clash)),
                "airport:MIA",
            ),
        )
    with pytest.raises(ServiceError, match="REQUIRED_CHECK_FAIL"):
        d01_store.review(run.run_id, approve(run), "key-1", "duty-manager", review_plan)
    assert resource(d01_store).data == clash
    assert d01_store.get_run(run.run_id).approval_state == Approval.PENDING


def test_current_snapshot_reports_which_rules_the_existing_booking_breaks():
    snapshot = d01()
    plan = next(
        e.plan
        for e in planning.build_catalog(snapshot).candidates
        if e.plan.candidate_id == "L960-G1-long"
    )
    busy = ResourceState(
        resource_id="airport:MIA",
        revision=1,
        data={
            APPLIED_KEY: [
                {
                    "booking_id": "applied:other",
                    "run_id": "other",
                    "landing_time_s": 960,
                    "runway_occupancy_s": 60,
                    "wake_category": "M",
                    "gate_id": "G1",
                    "gate_interval_s": [1500, 2700],
                }
            ]
        },
    )
    failed = {
        c.rule_id
        for c in planning.check_plan(current_snapshot(snapshot, busy), plan)
        if c.status != "PASS"
    }
    assert failed == {"ATC-RWY-01", "ATC-SEP-01", "GROUND-OCC-01"}
    assert all(c.status == "PASS" for c in planning.check_plan(snapshot, plan))


def test_booking_already_in_a_revised_snapshot_is_not_added_twice():
    snapshot = d01()
    entry = {
        "booking_id": "BG2",
        "landing_time_s": 780,
        "runway_occupancy_s": 60,
        "wake_category": "M",
        "gate_id": "G1",
        "gate_interval_s": [0, 1500],
    }
    same = current_snapshot(
        snapshot, ResourceState(resource_id="airport:MIA", revision=1, data={APPLIED_KEY: [entry]})
    )
    facts = {f.fact_id: f.value for f in same.facts}
    assert [m["movement_id"] for m in facts["overlay.movements"]] == ["BG1", "BG2"]
    assert facts["overlay.gates"][0]["reservations"] == [[0, 1500]]


def test_hook_is_pure_and_refuses_double_application(d01_store):
    run = pending_run(d01_store)
    state = resource(d01_store)
    effect = review_plan(run, state, approve(run))
    assert state.data == {}  # input untouched; only the returned data carries the booking
    booked = ResourceState(resource_id="airport:MIA", revision=1, data=effect.resource_data)
    with pytest.raises(ServiceError, match="ALREADY_APPLIED"):
        review_plan(run, booked, approve(run))


@pytest.mark.parametrize("data", [{APPLIED_KEY: "not-a-list"}, {APPLIED_KEY: [1, 2]}])
def test_malformed_resource_state_is_refused(d01_store, data):
    run = pending_run(d01_store)
    with pytest.raises(ServiceError, match="RESOURCE_STATE_INVALID"):
        review_plan(
            run, ResourceState(resource_id="airport:MIA", revision=1, data=data), approve(run)
        )


def test_dashboard_api_flow_with_the_discovered_hook(d01_store, tmp_path):
    settings = Settings(
        db_path=d01_store.path,
        scenario_dir=tmp_path / "none",
        evaluation_path=tmp_path / "latest.json",
        session_token=TOKEN,
    )
    app = create_app(settings=settings, store=d01_store, planner=HOOKS, provider=FixtureProvider())
    with TestClient(app) as client:
        assert client.get("/health").json()["review_connected"] is True
        started = client.post(
            "/api/runs",
            json={"scenario_id": "D01", "scenario_revision": 1},
            headers={**AUTH, "Idempotency-Key": "start-1"},
        )
        run_id = started.json()["run_id"]
        for _ in range(400):
            run = client.get(f"/api/runs/{run_id}").json()
            if run["processing_state"] == "TERMINAL":
                break
            time.sleep(0.01)
        body = {
            "decision": "APPROVE",
            "plan_hash": run["plan_hash"],
            "scenario_revision": 1,
            "resource_revision": 1,
            "ruleset_version": "toy-rules-v1",
            "policy_version": "balanced-v1",
            "expires_at": run["expires_at"],
            "reason": "Checked",
            "acknowledge_fallback": False,
        }
        assert (
            client.post(
                f"/api/runs/{run_id}/decision", json=body, headers={"Idempotency-Key": "d-1"}
            ).status_code
            == 403
        )
        ok = client.post(
            f"/api/runs/{run_id}/decision", json=body, headers={**AUTH, "Idempotency-Key": "d-1"}
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["approval_state"] == "APPLIED_SIMULATION"
        current = client.get("/api/resources/airport:MIA").json()
        assert current["revision"] == 2 and len(current["data"][APPLIED_KEY]) == 1
