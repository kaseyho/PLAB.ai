import time

import pytest
from fastapi.testclient import TestClient

from app.contracts import Approval, ReviewEffect, ServiceError
from app.main import Settings, create_app
from examples.backend_fixture import (
    FixtureProvider,
    fixture_checks,
    fixture_planner,
    fixture_review,
)

TOKEN = "test-session-with-at-least-16-characters"
HEADERS = {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "run-start"}


def api(store, tmp_path, **kwargs):
    settings = Settings(
        db_path=store.path,
        scenario_dir=tmp_path / "scenarios",
        static_dir=tmp_path / "static",
        evaluation_path=tmp_path / "latest.json",
        session_token=TOKEN,
    )
    return TestClient(
        create_app(
            settings=settings,
            store=store,
            planner=kwargs.get("planner", fixture_planner()),
            reviewer=kwargs.get("reviewer", fixture_review),
            provider=FixtureProvider(),
        )
    )


def ready(client, key="run-start"):
    response = client.post(
        "/api/runs",
        json={"scenario_id": "backend-fixture", "scenario_revision": 1},
        headers={**HEADERS, "Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    for _ in range(200):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["processing_state"] == "TERMINAL":
            return run
        time.sleep(0.005)
    raise AssertionError("Run did not terminate")


def decision_body(run, decision="APPROVE"):
    return {
        "decision": decision,
        "plan_hash": run["plan_hash"],
        "scenario_revision": run["snapshot"]["revision"],
        "resource_revision": run["snapshot"]["resource_revision"],
        "ruleset_version": run["snapshot"]["ruleset_version"],
        "policy_version": run["snapshot"]["policy_version"],
        "expires_at": run["expires_at"],
        "reason": "Reviewed fixture evidence",
        "acknowledge_fallback": False,
    }


def test_api_trace_and_single_atomic_application(store, tmp_path):
    with api(store, tmp_path) as client:
        run = ready(client)
        assert run["negotiation_outcome"] == "FEASIBLE_CONSENSUS"
        assert client.get("/health").json()["provider_mode"] == "FIXTURE"
        assert client.get("/api/evaluations/latest").json()["status"] == "NOT_YET_EVALUATED"
        assert store.get_resource("fixture-airport").revision == 1
        path = f"/api/runs/{run['run_id']}/decision"
        body = decision_body(run)
        headers = {**HEADERS, "Idempotency-Key": "approve-1"}
        approved = client.post(path, json=body, headers=headers)
        assert approved.status_code == 200, approved.text
        assert approved.json()["approval_state"] == "APPLIED_SIMULATION"
        assert client.post(path, json=body, headers=headers).json() == approved.json()
        assert store.get_resource("fixture-airport").revision == 2
        assert (
            client.post(
                path, json=body, headers={**headers, "Idempotency-Key": "approve-2"}
            ).status_code
            == 409
        )
        conflict = client.post(path, json={**body, "decision": "REJECT"}, headers=headers)
        assert conflict.status_code == 409
        trace = client.get(f"/api/runs/{run['run_id']}/export")
        assert trace.status_code == 200
        assert "attachment" in trace.headers["content-disposition"]
        assert sum(e["event_type"] == "SIMULATION_APPLIED" for e in trace.json()["events"]) == 1
        assert TOKEN not in trace.text


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {**HEADERS, "Origin": "https://untrusted.example"}],
)
def test_mutations_require_trusted_session_and_origin(store, tmp_path, headers):
    with api(store, tmp_path) as client:
        response = client.post(
            "/api/runs",
            json={"scenario_id": "backend-fixture", "scenario_revision": 1},
            headers=headers,
        )
        assert response.status_code == 403
        assert store.get_resource("fixture-airport").revision == 1


def test_reject_preserves_resources(store, tmp_path):
    with api(store, tmp_path) as client:
        run = ready(client)
        response = client.post(
            f"/api/runs/{run['run_id']}/decision",
            json=decision_body(run, "REJECT"),
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["approval_state"] == "REJECTED"
        assert store.get_resource("fixture-airport").revision == 1


def test_cancel_pending_recommendation_blocks_future_application(store, tmp_path):
    with api(store, tmp_path) as client:
        run = ready(client)
        path = f"/api/runs/{run['run_id']}"
        first = client.post(path + "/cancel", headers=HEADERS)
        assert first.status_code == 200
        assert first.json()["approval_state"] == "CANCELLED"
        assert client.post(path + "/cancel", headers=HEADERS).json() == first.json()
        assert (
            client.post(path + "/decision", json=decision_body(run), headers=HEADERS).status_code
            == 409
        )
        assert store.get_resource("fixture-airport").revision == 1


def test_two_concurrent_review_transactions_can_apply_only_one_run(store, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from app.contracts import ReviewRequest
    from app.store import Store

    with api(store, tmp_path) as client:
        runs = [ready(client, "first"), ready(client, "second")]

        def approve(run):
            try:
                result = Store(store.path).review(
                    run["run_id"],
                    ReviewRequest.model_validate(decision_body(run)),
                    run["run_id"],
                    "reviewer",
                    fixture_review,
                )
                return result.approval_state
            except ServiceError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(approve, runs))
        assert outcomes.count(Approval.APPLIED) == 1
        assert outcomes.count("REVIEW_NOT_PENDING") == 1
        assert store.get_resource("fixture-airport").revision == 2


def test_competing_run_is_invalidated_by_first_application(store, tmp_path):
    with api(store, tmp_path) as client:
        first, second = ready(client, "first"), ready(client, "second")
        assert (
            client.post(
                f"/api/runs/{first['run_id']}/decision", json=decision_body(first), headers=HEADERS
            ).status_code
            == 200
        )
        response = client.post(
            f"/api/runs/{second['run_id']}/decision",
            json=decision_body(second),
            headers={**HEADERS, "Idempotency-Key": "second-decision"},
        )
        assert response.status_code == 409
        assert store.get_run(second["run_id"]).approval_state == Approval.STALE
        assert store.get_resource("fixture-airport").revision == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("plan_hash", "wrong"),
        ("resource_revision", 8),
        ("ruleset_version", "other"),
        ("scenario_revision", 5),
        ("policy_version", "other"),
    ],
)
def test_wrong_review_binding_is_blocked(store, tmp_path, field, value):
    with api(store, tmp_path) as client:
        run = ready(client)
        body = {**decision_body(run), field: value}
        response = client.post(f"/api/runs/{run['run_id']}/decision", json=body, headers=HEADERS)
        assert response.status_code == 409
        assert store.get_resource("fixture-airport").revision == 1


def test_atomic_hook_failure_rolls_back_all_changes(store, tmp_path):
    def failing_hook(run, resource, request):
        raise ServiceError("REVALIDATION_FAILED")

    with api(store, tmp_path, reviewer=failing_hook) as client:
        run = ready(client)
        response = client.post(
            f"/api/runs/{run['run_id']}/decision", json=decision_body(run), headers=HEADERS
        )
        assert response.status_code == 409
        assert store.get_resource("fixture-airport").revision == 1
        assert store.get_run(run["run_id"]).approval_state == Approval.PENDING
        assert not any(e.event_type == "SIMULATION_APPLIED" for e in store.events(run["run_id"]))


def test_hook_cannot_apply_an_invalid_check_set(store, tmp_path):
    def invalid_hook(run, resource, request):
        checks = fixture_checks(run.snapshot, run.candidate)
        checks = (checks[0].model_copy(update={"status": "UNKNOWN"}), *checks[1:])
        return ReviewEffect(checks=checks, resource_data={"bad": True})

    with api(store, tmp_path, reviewer=invalid_hook) as client:
        run = ready(client)
        response = client.post(
            f"/api/runs/{run['run_id']}/decision", json=decision_body(run), headers=HEADERS
        )
        assert response.status_code == 409
        assert store.get_resource("fixture-airport").data == {}


def test_missing_integrations_are_explicit_not_faked(tmp_path):
    settings = Settings(
        db_path=tmp_path / "missing.sqlite3", session_token=TOKEN, scenario_dir=tmp_path / "none"
    )
    with TestClient(create_app(settings=settings, provider=FixtureProvider())) as client:
        assert client.get("/health").json()["planning_connected"] is False
        response = client.post(
            "/api/runs", json={"scenario_id": "absent", "scenario_revision": 1}, headers=HEADERS
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "PLANNING_NOT_CONNECTED"


def test_same_start_key_never_launches_another_run(store, tmp_path):
    with api(store, tmp_path) as client:
        first, second = ready(client), ready(client)
        assert first["run_id"] == second["run_id"]
        assert len(store.export(first["run_id"])["requests"]) == 6
        response = client.post(
            "/api/runs",
            json={"scenario_id": "backend-fixture", "scenario_revision": 2},
            headers=HEADERS,
        )
        assert response.status_code == 409


def test_revision_marks_review_stale_and_policy_edits_are_rejected(store, tmp_path):
    with api(store, tmp_path) as client:
        run = ready(client)
        new_snapshot = {**run["snapshot"], "revision": 2, "resource_revision": 2}
        response = client.post(
            "/api/scenarios/backend-fixture/revisions", json=new_snapshot, headers=HEADERS
        )
        assert response.status_code == 201, response.text
        assert store.get_run(run["run_id"]).approval_state == Approval.STALE
        changed_requirements = {
            **new_snapshot,
            "revision": 3,
            "resource_revision": 3,
            "required_fact_ids": ["replacement-fact"],
        }
        assert (
            client.post(
                "/api/scenarios/backend-fixture/revisions",
                json=changed_requirements,
                headers=HEADERS,
            ).status_code
            == 422
        )
        edited_policy = {
            **new_snapshot,
            "revision": 3,
            "resource_revision": 3,
            "ruleset_version": "waived",
        }
        assert (
            client.post(
                "/api/scenarios/backend-fixture/revisions", json=edited_policy, headers=HEADERS
            ).status_code
            == 422
        )


def test_static_dashboard_mount_and_validation_errors(store, tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>Developer A fixture</h1>")
    (static / "app.js").write_text("console.log('fixture');")
    with api(store, tmp_path) as client:
        assert "Developer A fixture" in client.get("/").text
        assert client.get("/static/app.js").status_code == 200
        response = client.post("/api/runs", json={"secret": "DO_NOT_ECHO"}, headers=HEADERS)
        assert response.status_code == 422
        assert "DO_NOT_ECHO" not in response.text
        assert client.get("/api/runs/missing").status_code == 404


def test_review_expiry_never_applies(store, tmp_path):
    from app.coordinator import Limits

    settings = Settings(db_path=store.path, session_token=TOKEN, scenario_dir=tmp_path / "none")
    app = create_app(
        settings=settings,
        store=store,
        planner=fixture_planner(),
        reviewer=fixture_review,
        provider=FixtureProvider(),
        limits=Limits(recommendation_ttl_s=0.001),
    )
    with TestClient(app) as client:
        run = ready(client)
        time.sleep(0.005)
        assert client.get(f"/api/runs/{run['run_id']}").json()["approval_state"] == "EXPIRED"
        assert (
            client.post(
                f"/api/runs/{run['run_id']}/decision", json=decision_body(run), headers=HEADERS
            ).status_code
            == 409
        )
        assert store.get_resource("fixture-airport").revision == 1
