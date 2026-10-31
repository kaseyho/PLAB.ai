"""Regenerate the dashboard's labelled sample states through the real API.

    uv run python app/static/fixtures/build_fixtures.py

Each state is produced by Developer B's API with the integrated D01 planner, the
real flights.csv record 71 and this repository's review hook. Agent messages come
from the offline FixtureProvider or an unavailable provider, never a live model;
`provider.mode` in every sample says so.
"""

import copy
import json
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import planning, scenarios  # noqa: E402
from app.agents import UnavailableProvider  # noqa: E402
from app.contracts import PlanningHooks, ResourceState  # noqa: E402
from app.main import Settings, create_app  # noqa: E402
from app.store import Store  # noqa: E402
from examples.backend_fixture import FixtureProvider  # noqa: E402

OUT = Path(__file__).resolve().parent
TOKEN = "fixture-builder-session-token"
AUTH = {"Authorization": "Bearer " + TOKEN}
HOOKS = PlanningHooks(planning.build_catalog, planning.check_plan, planning.select_fallback)
SOURCE = planning.load_snapshot(ROOT / "scenarios" / "D01.json", ROOT / "data").source
Mutation = Callable[[dict[str, Any]], None] | None
D01 = json.loads((ROOT / "scenarios" / "D01.json").read_text())


def scenario(mutate: Mutation) -> scenarios.ScenarioSnapshot:
    raw = copy.deepcopy(D01)
    if mutate:
        mutate(raw)
    return scenarios.snapshot_from_dict(raw)


def client(mutate: Mutation, provider: Any, workdir: Path) -> TestClient:
    store = Store(workdir / "fixture.sqlite3")
    snapshot = planning.snapshot_from_definition(scenario(mutate), SOURCE)
    store.register_scenario(snapshot, ResourceState(resource_id=snapshot.resource_id, revision=1))
    settings = Settings(
        db_path=store.path,
        scenario_dir=workdir / "none",
        evaluation_path=workdir / "latest.json",
        session_token=TOKEN,
    )
    return TestClient(create_app(settings=settings, store=store, planner=HOOKS, provider=provider))


def start(api: TestClient, key: str) -> dict[str, Any]:
    response = api.post(
        "/api/runs",
        json={"scenario_id": "D01", "scenario_revision": 1},
        headers={**AUTH, "Idempotency-Key": key},
    )
    response.raise_for_status()
    run_id = response.json()["run_id"]
    for _ in range(500):
        run: dict[str, Any] = api.get(f"/api/runs/{run_id}").json()
        if run["processing_state"] == "TERMINAL":
            return run
        time.sleep(0.01)
    raise RuntimeError("run did not finish")


def decide(api: TestClient, run: dict[str, Any], decision: str, reason: str) -> None:
    body = {
        "decision": decision,
        "plan_hash": run["plan_hash"],
        "scenario_revision": run["scenario_revision"],
        "resource_revision": run["resource_revision"],
        "ruleset_version": run["ruleset_version"],
        "policy_version": run["policy_version"],
        "expires_at": run["expires_at"],
        "reason": reason,
        "acknowledge_fallback": run["negotiation_outcome"] == "FEASIBLE_FALLBACK",
    }
    response = api.post(
        f"/api/runs/{run['run_id']}/decision",
        json=body,
        headers={**AUTH, "Idempotency-Key": f"decide-{run['run_id']}"},
    )
    response.raise_for_status()


def capture(api: TestClient, run_id: str, label: str) -> dict[str, Any]:
    run = api.get(f"/api/runs/{run_id}").json()
    if run["catalog"]:
        # Keep the samples small: shortlist entries only, plus the counts the dashboard shows.
        catalog = run["catalog"]
        everything = catalog["candidates"]
        blockers: dict[str, int] = {}
        for entry in everything:
            for check in entry["checks"]:
                if check["status"] != "PASS":
                    blockers[check["rule_id"]] = blockers.get(check["rule_id"], 0) + 1
        catalog["sample_summary"] = {
            "candidates": len(everything),
            "feasible": sum(all(c["status"] == "PASS" for c in e["checks"]) for e in everything),
            "blockers": dict(sorted(blockers.items())),
        }
        catalog["candidates"] = [
            e for e in everything if e["plan"]["candidate_id"] in catalog["shortlist_ids"]
        ]
    return {
        "sample_label": label,
        "run": run,
        "events": api.get(f"/api/runs/{run_id}/events").json(),
        "resource": api.get(f"/api/resources/{run['snapshot']['resource_id']}").json(),
        "evaluation": api.get("/api/evaluations/latest").json(),
    }


def state(name: str, mutate: Mutation = None, provider: Any = None) -> dict[str, Any]:
    with (
        tempfile.TemporaryDirectory() as tmp,
        client(mutate, provider or FixtureProvider(), Path(tmp)) as api,
    ):
        run = start(api, "start-1")
        label = (
            f"Sample '{name}': real API, planner and checks; "
            "agent messages from an offline test provider."
        )
        if name == "applied":
            decide(api, run, "APPROVE", "Margins and evidence checked")
        elif name == "rejected":
            decide(api, run, "REJECT", "Prefer to hold for G2 crew")
        elif name == "stale":
            winner = start(api, "start-2")
            decide(api, winner, "APPROVE", "Competing plan approved first")
        return capture(api, run["run_id"], label)


def no_fuel(raw: dict[str, Any]) -> None:
    raw["overlay"]["fuel"]["projected_fuel_at_earliest_kg"] = 0


def missing_fuel(raw: dict[str, Any]) -> None:
    raw["overlay"]["fuel"]["projected_fuel_at_earliest_kg"] = None


SAMPLES: dict[str, Callable[[], dict[str, Any]]] = {
    "consensus": lambda: state("consensus"),
    "applied": lambda: state("applied"),
    "rejected": lambda: state("rejected"),
    "stale": lambda: state("stale"),
    "fallback": lambda: state("fallback", provider=UnavailableProvider()),
    "no-feasible": lambda: state("no-feasible", no_fuel),
    "insufficient-data": lambda: state("insufficient-data", missing_fuel),
}

if __name__ == "__main__":
    for sample, build in SAMPLES.items():
        payload = build()
        (OUT / f"{sample}.json").write_text(json.dumps(payload, indent=1) + "\n")
        run = payload["run"]
        chosen = (run["candidate"] or {}).get("candidate_id")
        print(sample, run["negotiation_outcome"], run["approval_state"], chosen)
