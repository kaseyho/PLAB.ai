import json

import pytest

from app.agents import build_prompt
from app.contracts import Role, ServiceError, State
from examples.backend_fixture import FixtureProvider, fixture_catalog, sample_snapshot
from tests.conftest import start_run


@pytest.mark.parametrize(
    "weather,late,qualifies",
    [
        ("15", "20", False),
        ("15.001", "0", True),
        ("0", "20.001", True),
        (None, "21", True),
        ("16", None, True),
        (None, None, False),
        ("NaN", "Infinity", False),
        ("bad", "-1", False),
    ],
)
def test_eligibility_uses_strict_thresholds_and_preserves_missingness(weather, late, qualifies):
    source = sample_snapshot().source
    source = source.model_copy(
        update={
            "original_fields": {
                **source.original_fields,
                "WEATHER_DELAY": weather,
                "LATE_AIRCRAFT_DELAY": late,
            }
        }
    )
    assert source.qualifies() is qualifies
    assert source.original_fields["WEATHER_DELAY"] == weather


def test_prompts_scope_private_facts_and_checks_to_each_role(store):
    provider = FixtureProvider()
    run = start_run(store, provider)
    shared = run.snapshot.facts[0]
    facts = (
        shared,
        *(
            shared.model_copy(
                update={
                    "fact_id": f"private-{role}",
                    "domain": role,
                    "value": f"private value for {role}",
                }
            )
            for role in Role
        ),
    )
    snapshot = run.snapshot.model_copy(update={"facts": facts})
    run = run.model_copy(update={"snapshot": snapshot, "catalog": fixture_catalog(snapshot)})
    for role in Role:
        prompt = build_prompt(run, role, 1)
        context = prompt["context"]
        assert {f["fact_id"] for f in context["facts"]} == {
            "fixture-assumptions",
            f"private-{role}",
        }
        assert {c["domain"] for c in context["checks"]} == {None, role}
        assert f"private value for {role}" in json.dumps(prompt)
        for other in set(Role) - {role}:
            assert f"private value for {other}" not in json.dumps(prompt)


def test_duplicate_request_is_rejected_before_count_increment(store):
    provider = FixtureProvider()
    run = start_run(store, provider)
    for phase in (State.VALIDATING, State.PREPARING, State.ROUND_1):
        store.transition(run.run_id, phase)
    store.begin_request(run.run_id, Role.ATC, 1, {})
    with pytest.raises(ServiceError, match="DUPLICATE_GENERATION_REQUEST"):
        store.begin_request(run.run_id, Role.ATC, 1, {})
    with pytest.raises(ServiceError, match="REQUEST_BUDGET_OR_STATE"):
        store.begin_request(run.run_id, Role.PILOT, 3, {})
    assert store.get_run(run.run_id).agent_invocation_count == 1
