import copy
import json
from pathlib import Path

import pytest

from app.contracts import ResourceState, StartRunRequest
from app.scenarios import ScenarioSnapshot, snapshot_from_dict
from app.store import Store
from examples.backend_fixture import sample_snapshot


@pytest.fixture
def store(tmp_path: Path) -> Store:
    result = Store(tmp_path / "test.sqlite3")
    scenario = sample_snapshot()
    result.register_scenario(scenario, ResourceState(resource_id=scenario.resource_id, revision=1))
    return result


def start_run(store, provider, key="start", **kwargs):
    return store.create_run(
        StartRunRequest(scenario_id="backend-fixture", scenario_revision=1),
        key,
        provider.info,
        "FIXTURE",
        kwargs.get("deadline_s", 45),
    )[0]


ROOT = Path(__file__).resolve().parent.parent

D01 = json.loads((ROOT / "scenarios" / "D01.json").read_text())


def scenario(mutate=None) -> ScenarioSnapshot:
    """The Appendix A fixture, optionally edited by `mutate(raw_dict)` before validation."""
    raw = copy.deepcopy(D01)
    if mutate:
        mutate(raw)
    return snapshot_from_dict(raw)


def result(results, rule_id):
    return next(r for r in results if r.rule_id == rule_id)
