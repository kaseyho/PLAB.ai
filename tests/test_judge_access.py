import time

from fastapi.testclient import TestClient

from app.judge_access import COOKIE_NAME
from app.main import Settings, create_app
from examples.backend_fixture import (
    FixtureProvider,
    fixture_planner,
    fixture_review,
    sample_snapshot,
)
from tests.test_api import decision_body


def judge_app(tmp_path, monkeypatch, daily_limit=100):
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    (scenarios / "demo.json").write_text("{}")
    monkeypatch.setattr("app.planning.load_snapshot", lambda *_: sample_snapshot())
    return create_app(
        settings=Settings(
            db_path=tmp_path / "judge.sqlite3",
            scenario_dir=scenarios,
            public_demo=True,
            session_token="test-server-secret-with-at-least-32-characters",
            public_origin="http://testserver",
            daily_run_limit=daily_limit,
        ),
        provider=FixtureProvider(),
        planner=fixture_planner(),
        reviewer=fixture_review,
    )


def start_judge_run(client, scenario, key="judge-start"):
    response = client.post(
        "/api/runs",
        json={"scenario_id": scenario["scenario_id"], "scenario_revision": scenario["revision"]},
        headers={"Origin": "http://testserver", "Idempotency-Key": key},
    )
    if response.status_code != 202:
        return response, None
    run_id = response.json()["run_id"]
    for _ in range(200):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["processing_state"] == "TERMINAL":
            return response, run
        time.sleep(0.005)
    raise AssertionError("Judge run did not finish")


def test_judges_get_isolated_runs_and_approve_without_api_keys(tmp_path, monkeypatch):
    app = judge_app(tmp_path, monkeypatch)
    with TestClient(app) as alice:
        bob = TestClient(app)
        assert alice.get("/api/scenarios").status_code == 403
        assert alice.get("/api/session").json() == {"public_demo": True}
        assert "HttpOnly" in alice.get("/api/session").headers["set-cookie"]
        bob.get("/api/session")
        a = alice.get("/api/scenarios").json()[0]
        b = bob.get("/api/scenarios").json()[0]
        assert a["resource_id"] != b["resource_id"]
        response, run = start_judge_run(alice, a)
        assert response.status_code == 202
        assert run["negotiation_outcome"] == "FEASIBLE_CONSENSUS"
        assert bob.get(f"/api/runs/{run['run_id']}").status_code == 404
        assert bob.get(f"/api/resources/{a['resource_id']}").status_code == 404
        assert bob.get(f"/api/scenarios/{a['scenario_id']}").status_code == 404
        approved = alice.post(
            f"/api/runs/{run['run_id']}/decision",
            json=decision_body(run),
            headers={"Origin": "http://testserver", "Idempotency-Key": "approval"},
        )
        assert approved.json()["approval_state"] == "APPLIED_SIMULATION"
        assert bob.get(f"/api/resources/{b['resource_id']}").json()["revision"] == 1
        assert "test-server-secret" not in alice.get(f"/api/runs/{run['run_id']}/export").text
        alice.cookies.set(COOKIE_NAME, "forged", domain="testserver.local", path="/")
        assert alice.get("/api/scenarios").status_code == 403


def test_public_budget_and_origin_checks_preserve_retries(tmp_path, monkeypatch):
    with TestClient(judge_app(tmp_path, monkeypatch, daily_limit=1)) as client:
        client.get("/api/session")
        scenario = client.get("/api/scenarios").json()[0]
        assert (
            client.post(
                "/api/runs",
                json={"scenario_id": scenario["scenario_id"], "scenario_revision": 1},
                headers={"Origin": "https://untrusted.example", "Idempotency-Key": "bad"},
            ).status_code
            == 403
        )
        original, _ = start_judge_run(client, scenario)
        retry, _ = start_judge_run(client, scenario)
        assert retry.json()["run_id"] == original.json()["run_id"]
        limited, _ = start_judge_run(client, scenario, "second")
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "JUDGE_RUN_LIMIT"
