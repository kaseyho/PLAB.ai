from pathlib import Path

import pytest

from app.contracts import ResourceState, StartRunRequest
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
