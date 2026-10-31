import json

import pytest

from app.agents import ProviderReply
from app.contracts import (
    Approval,
    Outcome,
    ResourceState,
    Role,
    StartRunRequest,
    verify_event_chain,
)
from app.coordinator import Coordinator
from app.evaluate import PLANNER
from app.planning import snapshot_from_definition
from app.store import Store
from examples.backend_fixture import FixtureProvider, sample_snapshot
from tests.test_organizer_rules import native


class DissentProvider(FixtureProvider):
    def __init__(self, final="ACCEPT"):
        self.final = final
        self.first_hash = None
        self.third_prompts = []

    async def generate(self, role, round_number, prompt, timeout_s):
        reply = await super().generate(role, round_number, prompt, timeout_s)
        body = json.loads(reply.text)
        if round_number == 2:
            self.first_hash = body["plan_hash"]
            if role == Role.PILOT:
                body["decision"] = "REJECT"
        if round_number == 3:
            self.third_prompts.append(prompt["context"])
            if role == Role.PILOT:
                if self.final == "old_hash":
                    body["plan_hash"] = self.first_hash
                else:
                    body["decision"] = self.final
        return ProviderReply(json.dumps(body))


@pytest.mark.parametrize(
    "final,outcome,reason",
    [
        ("ACCEPT", Outcome.CONSENSUS, None),
        ("REJECT", Outcome.FALLBACK, "THREE_TURN_NO_CONSENSUS"),
        ("NEEDS_INFORMATION", Outcome.FALLBACK, "THREE_TURN_NO_CONSENSUS"),
        ("old_hash", Outcome.FALLBACK, "VOTE_PLAN_HASH_MISMATCH"),
    ],
)
async def test_third_turn_is_fresh_bounded_and_persisted(tmp_path, final, outcome, reason):
    store = Store(tmp_path / "test.sqlite3")
    snapshot = snapshot_from_definition(native(), sample_snapshot().source)
    store.register_scenario(snapshot, ResourceState(resource_id=snapshot.resource_id, revision=1))
    provider = DissentProvider(final)
    run, _ = store.create_run(
        StartRunRequest(scenario_id=snapshot.scenario_id, scenario_revision=1),
        "three-turn",
        provider.info,
        "INTEGRATED",
        45,
    )
    result = await Coordinator(store, PLANNER, provider).run(run.run_id)
    assert result.negotiation_outcome == outcome
    assert result.fallback_reason == reason
    assert result.round_count == 3
    assert result.agent_invocation_count == 9
    assert result.model_call_count == 0
    assert result.approval_state == Approval.PENDING
    assert store.get_resource(snapshot.resource_id).data == {}
    assert len(provider.third_prompts) == 3
    assert {p["plan_hash"] for p in provider.third_prompts} != {provider.first_hash}
    assert all(p["previous_votes"]["PILOT"]["decision"] == "REJECT" for p in provider.third_prompts)
    if outcome == Outcome.CONSENSUS:
        assert set(result.votes) == set(Role)
        assert all(v.plan_hash == result.plan_hash for v in result.votes.values())
    trace = store.export(run.run_id)
    assert len(trace["requests"]) == 9
    assert len({(r["round"], r["role"]) for r in trace["requests"]}) == 9
    assert {m["round"] for m in trace["messages"]} == {1, 2, 3}
    assert verify_event_chain(store.events(run.run_id))
    assert Store(store.path).get_run(run.run_id) == result
