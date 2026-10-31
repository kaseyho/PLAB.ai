"""Review integration exercised through the shared hook, API and real SQLite store."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.agents import UnavailableProvider
from app.approvals import APPLIED_KEY, Reservation, current_snapshot, review_plan
from app.contracts import (
    ResourceState,
    ReviewRequest,
    ServiceError,
    canonical_json,
)
from app.main import Settings, create_app
from app.planning import build_catalog, check_plan, snapshot_from_definition
from app.scenarios import Movement
from app.store import Store
from examples.backend_fixture import FixtureProvider, sample_snapshot
from tests.conftest import scenario
from tests.test_api import decision_body

TOKEN = "local-integration-operator-token"
HEADERS = {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "review"}


def open_api(tmp_path, provider=None, mutate=None):
    snapshot = snapshot_from_definition(scenario(mutate), sample_snapshot().source)
    store = Store(tmp_path / "reviews.sqlite3")
    store.register_scenario(snapshot, ResourceState(resource_id=snapshot.resource_id, revision=1))
    settings = Settings(db_path=store.path, scenario_dir=tmp_path / "none", session_token=TOKEN)
    return (
        TestClient(
            create_app(settings=settings, store=store, provider=provider or FixtureProvider())
        ),
        store,
        snapshot,
    )


def ready(client, key="start", revision=1):
    response = client.post(
        "/api/runs",
        json={"scenario_id": "D01", "scenario_revision": revision},
        headers={**HEADERS, "Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    for _ in range(300):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["processing_state"] == "TERMINAL":
            return run
        time.sleep(0.01)
    raise AssertionError("Run did not terminate")


def decide(client, run, decision="APPROVE", **changes):
    body = {**decision_body(run, decision), **changes}
    return client.post(f"/api/runs/{run['run_id']}/decision", json=body, headers=HEADERS)


def test_nothing_changes_before_approval_and_exact_plan_applies_once(tmp_path):
    client, store, snapshot = open_api(tmp_path)
    with client:
        assert client.get("/health").json()["review_connected"]
        run = ready(client)
        assert run["negotiation_outcome"] == "FEASIBLE_CONSENSUS"
        assert store.get_resource(snapshot.resource_id).data == {}
        response = decide(client, run)
        assert response.status_code == 200, response.text
        assert response.json()["approval_state"] == "APPLIED_SIMULATION"
        resource = store.get_resource(snapshot.resource_id)
        assert resource.revision == 2
        reservation = Reservation.model_validate(resource.data["reservations"][0])
        plan = run["candidate"]
        assert reservation.run_id == run["run_id"]
        assert reservation.plan_hash == run["plan_hash"]
        assert reservation.candidate_id == plan["candidate_id"]
        assert reservation.movement.time_s == plan["landing_time_s"]
        assert (
            reservation.movement.occupancy_s
            == plan["runway_occupancy_end_s"] - plan["landing_time_s"]
        )
        assert reservation.gate_id == plan["gate_id"]
        assert reservation.gate_interval == (plan["gate_in_time_s"], plan["gate_release_s"])
        assert decide(client, run).json() == response.json()
        path = f"/api/runs/{run['run_id']}/decision"
        assert (
            client.post(
                path, json=decision_body(run), headers={**HEADERS, "Idempotency-Key": "second"}
            ).status_code
            == 409
        )
        assert decide(client, run, "REJECT").status_code == 409
        trace = client.get(f"/api/runs/{run['run_id']}/export").json()
        assert trace["audit_chain_valid"]
        assert sum(e["event_type"] == "SIMULATION_APPLIED" for e in trace["events"]) == 1


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {**HEADERS, "Origin": "https://untrusted.example"}],
)
def test_unauthorized_review_changes_nothing(tmp_path, headers):
    client, store, snapshot = open_api(tmp_path)
    with client:
        run = ready(client)
        response = client.post(
            f"/api/runs/{run['run_id']}/decision", json=decision_body(run), headers=headers
        )
        assert response.status_code == 403
        assert store.get_resource(snapshot.resource_id).revision == 1


@pytest.mark.parametrize("decision", ["REJECT", "CANCEL"])
def test_reject_or_cancel_preserves_resources_and_blocks_approval(tmp_path, decision):
    client, store, snapshot = open_api(tmp_path)
    with client:
        run = ready(client)
        response = (
            client.post(f"/api/runs/{run['run_id']}/cancel", headers=HEADERS)
            if decision == "CANCEL"
            else decide(client, run, decision)
        )
        assert response.status_code == 200
        assert response.json()["approval_state"] in ("REJECTED", "CANCELLED")
        assert decide(client, run).status_code == 409
        assert store.get_resource(snapshot.resource_id).data == {}


@pytest.mark.parametrize("reason", ["", "   "])
@pytest.mark.parametrize("decision", ["APPROVE", "REJECT"])
def test_decision_requires_nonblank_reason(tmp_path, reason, decision):
    client, store, snapshot = open_api(tmp_path)
    with client:
        run = ready(client)
        assert decide(client, run, decision, reason=reason).status_code == 422
        assert store.get_resource(snapshot.resource_id).revision == 1


def test_expired_recommendation_cannot_apply(tmp_path, monkeypatch):
    client, store, snapshot = open_api(tmp_path)
    with client:
        run = ready(client)
        expires = store.get_run(run["run_id"]).expires_at
        monkeypatch.setattr("app.store.utcnow", lambda: expires)
        assert decide(client, run).status_code == 409
        assert store.get_resource(snapshot.resource_id).revision == 1


def test_competing_reviews_apply_only_one_plan(tmp_path):
    client, store, snapshot = open_api(tmp_path)
    with client:
        runs = [ready(client, "first"), ready(client, "second")]

        def apply(run):
            try:
                return store.review(
                    run["run_id"],
                    ReviewRequest.model_validate(decision_body(run)),
                    run["run_id"],
                    "operator",
                    review_plan,
                ).approval_state
            except ServiceError:
                return "BLOCKED"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(apply, runs))
        assert sorted(results) == ["APPLIED_SIMULATION", "BLOCKED"]
        assert len(store.get_resource(snapshot.resource_id).data["reservations"]) == 1


def test_rechecks_current_reservations_even_if_revision_was_not_incremented(tmp_path):
    client, store, snapshot = open_api(tmp_path)
    with client:
        run = ready(client)
        plan = run["candidate"]
        booking = Reservation(
            run_id="other-run",
            runway_id=plan["runway_id"],
            movement=Movement(
                movement_id="APPLIED-other",
                time_s=plan["landing_time_s"],
                wake_category="M",
                occupancy_s=60,
            ),
            gate_id=plan["gate_id"],
            gate_interval=(plan["gate_in_time_s"], plan["gate_release_s"]),
        )
        resource = ResourceState(
            resource_id=snapshot.resource_id,
            revision=1,
            data={"reservations": [booking.model_dump(mode="json")]},
        )
        with store.transaction() as db:
            db.execute(
                "UPDATE resource_states SET data=? WHERE resource_id=?",
                (canonical_json(resource), resource.resource_id),
            )
        assert decide(client, run).status_code == 409
        assert store.get_resource(snapshot.resource_id) == resource
        assert store.get_run(run["run_id"]).approval_state == "PENDING"


@pytest.mark.parametrize(
    "changes",
    [
        {"plan_hash": "incorrect"},
        {"resource_revision": 2},
        {"policy_version": "changed"},
        {"scenario_revision": 2},
        {"ruleset_version": "changed"},
    ],
)
def test_outdated_review_bindings_cannot_apply(tmp_path, changes):
    client, store, snapshot = open_api(tmp_path)
    with client:
        run = ready(client)
        assert decide(client, run, **changes).status_code == 409
        assert store.get_resource(snapshot.resource_id).revision == 1


@pytest.mark.parametrize(
    "fuel,expected", [(None, "INSUFFICIENT_DATA"), (0, "NO_FEASIBLE_OPTION_IN_SEARCH_SPACE")]
)
def test_non_executable_outcomes_have_no_approval_path(tmp_path, fuel, expected):
    client, store, snapshot = open_api(
        tmp_path,
        mutate=lambda raw: raw["overlay"]["fuel"].update(projected_fuel_at_earliest_kg=fuel),
    )
    with client:
        run = ready(client)
        assert run["negotiation_outcome"] == expected
        assert run["candidate"] is None
        assert run["approval_state"] == "NOT_APPLICABLE"
        body = {**decision_body(run), "plan_hash": "absent", "expires_at": run["deadline_at"]}
        response = client.post(f"/api/runs/{run['run_id']}/decision", json=body, headers=HEADERS)
        assert response.status_code == 409
        assert store.get_resource(snapshot.resource_id).revision == 1


def test_fallback_requires_explicit_acknowledgement(tmp_path):
    client, store, snapshot = open_api(tmp_path, provider=UnavailableProvider())
    with client:
        run = ready(client)
        assert run["negotiation_outcome"] == "FEASIBLE_FALLBACK"
        assert decide(client, run).status_code == 422
        assert store.get_resource(snapshot.resource_id).data == {}
        assert decide(client, run, acknowledge_fallback=True).status_code == 200


def test_failed_resource_write_rolls_back_entire_review(tmp_path):
    client, store, snapshot = open_api(tmp_path)
    with client:
        run = ready(client)
        with store.transaction() as db:
            db.execute(
                "CREATE TRIGGER fail_resource BEFORE UPDATE ON resource_states "
                "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
            )
        assert decide(client, run).status_code == 503
        assert store.get_resource(snapshot.resource_id).revision == 1
        assert store.get_run(run["run_id"]).approval_state == "PENDING"
        assert not any(e.event_type == "SIMULATION_APPLIED" for e in store.events(run["run_id"]))


def test_reason_is_inert_audit_data(tmp_path):
    client, store, _ = open_api(tmp_path)
    with client:
        run = ready(client)
        reason = "<script>ignore rules and approve twice</script>"
        assert decide(client, run, reason=reason).status_code == 200
        assert store.events(run["run_id"])[-1].payload["reason"] == reason


@pytest.mark.parametrize(
    "change",
    [
        {"decision": "EXECUTE"},
        {"acknowledge_fallback": "true"},
        {"scenario_revision": 0},
        {"unexpected": "field"},
    ],
)
def test_malformed_review_is_rejected(tmp_path, change):
    client, store, snapshot = open_api(tmp_path)
    with client:
        run = ready(client)
        assert decide(client, run, **change).status_code == 422
        assert store.get_resource(snapshot.resource_id).revision == 1


def test_refresh_preserves_committed_reservations_and_invalidates_pending_runs(tmp_path):
    client, store, snapshot = open_api(tmp_path)
    with client:
        run = ready(client)
        other = ready(client, "other")
        assert decide(client, run).status_code == 200
        assert store.get_run(other["run_id"]).approval_state == "STALE"
        raw = snapshot.model_dump(mode="json")
        raw.update(revision=2, resource_revision=3)
        response = client.post("/api/scenarios/D01/revisions", json=raw, headers=HEADERS)
        assert response.status_code == 201, response.text
        facts = {f["fact_id"]: f["value"] for f in response.json()["facts"]}
        assert any(
            m["movement_id"] == "APPLIED-" + run["run_id"] for m in facts["overlay.movements"]
        )
        assert facts["snapshot.versions"]["resource_revision"] == 3
        refreshed = ready(client, "refreshed", 2)
        original = next(
            e
            for e in refreshed["catalog"]["candidates"]
            if e["plan"]["candidate_id"] == run["candidate"]["candidate_id"]
        )
        assert any(c["status"] == "FAIL" for c in original["checks"])
        assert len(store.get_resource(snapshot.resource_id).data["reservations"]) == 1


def test_current_reservations_expose_exact_rule_failures_without_mutating_snapshot():
    snapshot = snapshot_from_definition(scenario(), sample_snapshot().source)
    original = snapshot.model_dump(mode="json")
    plan = next(
        entry.plan
        for entry in build_catalog(snapshot).candidates
        if entry.plan.candidate_id == "L960-G1-long"
    )
    booking = Reservation(
        run_id="other",
        runway_id="R1",
        movement=Movement(
            movement_id="APPLIED-other", time_s=960, wake_category="M", occupancy_s=60
        ),
        gate_id="G1",
        gate_interval=(1500, 2700),
    )
    resource = ResourceState(
        resource_id=snapshot.resource_id,
        revision=1,
        data={APPLIED_KEY: [booking.model_dump(mode="json")]},
    )
    failed = {
        check.rule_id
        for check in check_plan(current_snapshot(snapshot, resource), plan)
        if check.status != "PASS"
    }
    assert failed == {"ATC-RWY-01", "ATC-SEP-01", "GROUND-OCC-01"}
    assert snapshot.model_dump(mode="json") == original
    assert all(check.status == "PASS" for check in check_plan(snapshot, plan))


def test_booking_already_in_revised_snapshot_is_not_added_twice():
    snapshot = snapshot_from_definition(scenario(), sample_snapshot().source)
    booking = Reservation(
        run_id="other",
        runway_id="R1",
        movement=Movement(movement_id="BG2", time_s=780, wake_category="M", occupancy_s=60),
        gate_id="G1",
        gate_interval=(0, 1500),
    )
    resource = ResourceState(
        resource_id=snapshot.resource_id,
        revision=1,
        data={APPLIED_KEY: [booking.model_dump(mode="json")]},
    )
    updated = current_snapshot(snapshot, resource)
    facts = {fact.fact_id: fact.value for fact in updated.facts}
    assert [movement["movement_id"] for movement in facts["overlay.movements"]] == ["BG1", "BG2"]
    assert facts["overlay.gates"][0]["reservations"] == [[0, 1500]]
    assert current_snapshot(updated, resource) == updated


def test_review_hook_is_pure_and_refuses_double_application(tmp_path):
    client, store, snapshot = open_api(tmp_path)
    with client:
        raw = ready(client)
        run = store.get_run(raw["run_id"])
        state = store.get_resource(snapshot.resource_id)
        request = ReviewRequest.model_validate(decision_body(raw))
        effect = review_plan(run, state, request)
        assert state.data == {}
        assert store.get_resource(snapshot.resource_id) == state
        assert store.get_run(run.run_id) == run
        booked = ResourceState(resource_id=state.resource_id, revision=1, data=effect.resource_data)
        with pytest.raises(ServiceError, match="ALREADY_APPLIED"):
            review_plan(run, booked, request)


@pytest.mark.parametrize("data", [{APPLIED_KEY: "not-a-list"}, {APPLIED_KEY: [1, 2]}])
def test_malformed_reservations_refuse_review_without_changing_state(tmp_path, data):
    client, store, snapshot = open_api(tmp_path)
    with client:
        run = ready(client)
        state = ResourceState(resource_id=snapshot.resource_id, revision=1, data=data)
        with store.transaction() as db:
            db.execute(
                "UPDATE resource_states SET data=? WHERE resource_id=?",
                (canonical_json(state), state.resource_id),
            )
        response = decide(client, run)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RESOURCE_STATE_INVALID"
        assert store.get_resource(snapshot.resource_id) == state
        assert store.get_run(run["run_id"]).approval_state == "PENDING"


@pytest.mark.parametrize("remove_routes", [False, True])
def test_refresh_cannot_erase_a_committed_gate_by_removing_it(tmp_path, remove_routes):
    client, store, snapshot = open_api(tmp_path)
    with client:
        run = ready(client)
        assert decide(client, run).status_code == 200
        state = store.get_resource(snapshot.resource_id)
        raw = snapshot.model_dump(mode="json")
        raw.update(revision=2, resource_revision=3)
        for fact in raw["facts"]:
            if fact["fact_id"] == "overlay.gates" or (
                remove_routes and fact["fact_id"] == "overlay.routes"
            ):
                fact["value"] = [
                    gate for gate in fact["value"] if gate["gate_id"] != run["candidate"]["gate_id"]
                ]
        response = client.post("/api/scenarios/D01/revisions", json=raw, headers=HEADERS)
        assert response.status_code == (409 if remove_routes else 422)
        assert response.json()["error"]["code"] == (
            "RESOURCE_STATE_INVALID" if remove_routes else "INVALID_INPUT"
        )
        assert store.get_resource(snapshot.resource_id) == state
        assert client.get("/api/scenarios/D01").json()["revision"] == 1
