import json

import pytest

from app.agents import build_prompt
from app.contracts import (
    AgentMessage,
    AgentOffer,
    AgentVote,
    Outcome,
    Role,
    ServiceError,
    State,
    utcnow,
)
from examples.backend_fixture import FixtureProvider, fixture_catalog, fixture_checks
from tests.conftest import start_run


async def record_round(store, run_id, provider, number, roles):
    for role in roles:
        run = store.get_run(run_id)
        prompt = build_prompt(run, role, number)
        request_id = store.begin_request(run_id, role, number, prompt)
        reply = await provider.generate(role, number, prompt, 12)
        store.finish_request(request_id, {"status": "RECEIVED", "raw_output": reply.text})
        payload_type = AgentOffer if number == 1 else AgentVote
        store.record_message(
            AgentMessage(
                message_id=request_id,
                run_id=run_id,
                sender_role=role,
                round=number,
                message_type="OFFER" if number == 1 else "VOTE",
                scenario_revision=run.snapshot.revision,
                resource_revision=run.snapshot.resource_revision,
                ruleset_version=run.snapshot.ruleset_version,
                policy_version=run.snapshot.policy_version,
                candidate_set_hash=run.catalog.catalog_hash,
                payload=payload_type.model_validate_json(reply.text),
                received_at=utcnow(),
            )
        )


async def test_two_accepts_cannot_be_committed_as_consensus(store):
    provider = FixtureProvider()
    run = start_run(store, provider)
    store.transition(run.run_id, State.VALIDATING)
    store.transition(run.run_id, State.PREPARING)
    catalog = fixture_catalog(run.snapshot)
    store.save_catalog(run.run_id, catalog)
    store.transition(run.run_id, State.ROUND_1)
    await record_round(store, run.run_id, provider, 1, Role)
    store.transition(run.run_id, State.RECONCILING)
    plan = catalog.candidates[1].plan
    store.select_proposal(run.run_id, plan)
    store.transition(run.run_id, State.ROUND_2)
    await record_round(store, run.run_id, provider, 2, (Role.ATC, Role.PILOT))
    store.transition(run.run_id, State.FINAL_VALIDATION)
    with pytest.raises(ServiceError, match="UNANIMOUS_CURRENT_VOTES_REQUIRED"):
        store.complete(
            run.run_id, Outcome.CONSENSUS, candidate=plan, checks=fixture_checks(run.snapshot, plan)
        )
    assert store.get_run(run.run_id).negotiation_outcome is None


async def test_duplicate_envelope_and_crossed_versions_are_guarded(store):
    provider = FixtureProvider()
    run = start_run(store, provider)
    store.transition(run.run_id, State.VALIDATING)
    store.transition(run.run_id, State.PREPARING)
    store.save_catalog(run.run_id, fixture_catalog(run.snapshot))
    store.transition(run.run_id, State.ROUND_1)
    await record_round(store, run.run_id, provider, 1, (Role.ATC,))
    message = AgentMessage.model_validate(store.export(run.run_id)["messages"][0])
    assert store.record_message(message) is False
    conflict = message.model_copy(
        update={"payload": message.payload.model_copy(update={"rationale": "different offer"})}
    )
    with pytest.raises(ServiceError, match="CONFLICTING_DUPLICATE_MESSAGE"):
        store.record_message(conflict)
    crossed = message.model_copy(update={"sender_role": Role.PILOT, "scenario_revision": 2})
    with pytest.raises(ServiceError, match="MESSAGE_VERSION_MISMATCH"):
        store.record_message(crossed)
    assert set(store.get_run(run.run_id).offers) == {Role.ATC}
    assert "different offer" not in json.dumps(store.export(run.run_id)["messages"])
