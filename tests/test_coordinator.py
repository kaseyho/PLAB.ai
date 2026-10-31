import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from app.agents import ProviderReply, UnavailableProvider, build_prompt
from app.contracts import (
    REQUIRED_RULES,
    AgentMessage,
    Approval,
    CatalogResult,
    FallbackResult,
    Outcome,
    Role,
    ServiceError,
    State,
    utcnow,
    verify_event_chain,
)
from app.coordinator import Coordinator, Limits
from examples.backend_fixture import (
    FixtureProvider,
    fixture_catalog,
    fixture_checks,
    fixture_planner,
    sample_snapshot,
)
from tests.conftest import start_run


async def test_conflicting_preferences_reach_same_plan_with_six_invocations(store):
    provider = FixtureProvider()
    run = start_run(store, provider)
    result = await Coordinator(store, fixture_planner(), provider).run(run.run_id)
    assert result.negotiation_outcome == Outcome.CONSENSUS
    assert result.candidate.candidate_id == "C2"
    assert result.offers[Role.ATC].preferred_candidate_id == "C1"
    assert result.offers[Role.GROUND].preferred_candidate_id == "C2"
    assert {v.plan_hash for v in result.votes.values()} == {result.plan_hash}
    assert result.round_count == 2
    assert result.agent_invocation_count == 6
    assert result.model_call_count == 0  # Test doubles must never masquerade as model requests.
    assert result.approval_state == Approval.PENDING
    assert store.get_resource("fixture-airport").revision == 1
    trace = store.export(run.run_id)
    assert len(trace["requests"]) == len(trace["messages"]) == 6
    assert {r["role"] for r in trace["requests"]} == set(Role)
    assert all(r["prompt_hash"] and r["finished_at"] for r in trace["requests"])
    events = store.events(run.run_id)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert len(store.events(run.run_id, events[-2].sequence)) == 1


class FaultProvider(FixtureProvider):
    def __init__(self, fault, round_number=1):
        self.fault, self.round_number = fault, round_number
        self.calls = []

    async def generate(self, role, round_number, prompt, timeout_s):
        self.calls.append((role, round_number))
        reply = await super().generate(role, round_number, prompt, timeout_s)
        if role != Role.PILOT or round_number != self.round_number:
            return reply
        body = json.loads(reply.text)
        if self.fault == "invalid_json":
            return ProviderReply("not JSON")
        if self.fault == "invented_candidate":
            body["concession_candidate_ids"] = ["imaginary"]
        if self.fault == "invented_evidence":
            body["evidence_ids"] = ["fictional-fuel"]
        if self.fault == "extra_identity":
            body["sender_role"] = "ATC"
        if self.fault == "invalid_type":
            body["acceptable_candidate_ids"] = "C1"
        if self.fault == "wrong_hash":
            body["plan_hash"] = "0" * 64
        if self.fault in ("REJECT", "NEEDS_INFORMATION"):
            body["decision"] = self.fault
        if self.fault == "bad_order":
            body["ranked_candidate_ids"] = list(reversed(body["ranked_candidate_ids"]))
        if self.fault == "duplicate":
            body["concession_candidate_ids"] *= 2
        if self.fault == "empty_offer":
            body.update(
                preferred_candidate_id=None,
                acceptable_candidate_ids=[],
                concession_candidate_ids=[],
                ranked_candidate_ids=[],
            )
        if self.fault == "exception":
            raise RuntimeError("SECRET_MUST_NOT_ENTER_TRACE")
        if self.fault == "incomplete":
            return ProviderReply(reply.text, status="incomplete", output_tokens=500)
        if self.fault == "timeout":
            await asyncio.sleep(2)
        return ProviderReply(json.dumps(body))


@pytest.mark.parametrize(
    "fault,round_number,reason",
    [
        ("invalid_json", 1, "INVALID_AGENT_SCHEMA"),
        ("invented_candidate", 1, "CONCESSION_POLICY_VIOLATION"),
        ("invented_evidence", 1, "UNKNOWN_AGENT_EVIDENCE"),
        ("extra_identity", 1, "INVALID_AGENT_SCHEMA"),
        ("invalid_type", 1, "INVALID_AGENT_SCHEMA"),
        ("bad_order", 1, "OFFER_RANKING_MISMATCH"),
        ("duplicate", 1, "DUPLICATE_OFFER_ID"),
        ("empty_offer", 1, "NO_MUTUAL_OFFER"),
        ("exception", 1, "PROVIDER_ERROR"),
        ("incomplete", 1, "PROVIDER_INCOMPLETE_OR_REFUSED"),
        ("wrong_hash", 2, "VOTE_PLAN_HASH_MISMATCH"),
        ("REJECT", 2, "FINAL_VOTE_DISSENT"),
        ("NEEDS_INFORMATION", 2, "FINAL_VOTE_DISSENT"),
    ],
)
async def test_faults_end_in_explicit_fallback_without_repair_calls(
    store, fault, round_number, reason
):
    provider = FaultProvider(fault, round_number)
    run = start_run(store, provider)
    result = await Coordinator(store, fixture_planner(), provider).run(run.run_id)
    assert result.negotiation_outcome == Outcome.FALLBACK
    assert result.fallback_reason == reason
    assert result.candidate.candidate_id == "C1"
    assert result.round_count <= 2
    assert len(provider.calls) == result.agent_invocation_count <= 6
    assert len(set(provider.calls)) == len(provider.calls)
    assert "SECRET_MUST_NOT_ENTER_TRACE" not in json.dumps(store.export(run.run_id))


async def test_timeout_is_bounded_and_fallback_makes_no_provider_request(store):
    provider = FaultProvider("timeout")
    limits = Limits(
        request_timeout_s=0.04, deadline_s=1, fallback_reserve_s=0.3, planning_timeout_s=0.1
    )
    run = start_run(store, provider, deadline_s=1)
    result = await asyncio.wait_for(
        Coordinator(store, fixture_planner(), provider, limits).run(run.run_id), 1
    )
    assert result.negotiation_outcome == Outcome.FALLBACK
    assert result.fallback_reason == "AGENT_TIMEOUT"
    assert len(provider.calls) == 3
    assert any(r["status"] == "TIMEOUT" for r in store.export(run.run_id)["requests"])


async def test_unconfigured_provider_falls_back_with_zero_invocations(store):
    provider = UnavailableProvider()
    run = start_run(store, provider)
    result = await Coordinator(store, fixture_planner(), provider).run(run.run_id)
    assert result.negotiation_outcome == Outcome.FALLBACK
    assert result.fallback_reason == "PROVIDER_NOT_CONFIGURED"
    assert result.agent_invocation_count == result.model_call_count == 0


async def test_provider_ignoring_cancellation_cannot_extend_deadline_or_change_outcome(store):
    class StubbornProvider(FixtureProvider):
        def __init__(self):
            self.release = asyncio.Event()
            self.finished = 0
            self.all_finished = asyncio.Event()

        async def generate(self, role, round_number, prompt, timeout_s):
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                await self.release.wait()
            self.finished += 1
            if self.finished == 3:
                self.all_finished.set()
            reply = await super().generate(role, round_number, prompt, timeout_s)
            return replace(reply, input_tokens=100, output_tokens=50)

    provider = StubbornProvider()
    run = start_run(store, provider, deadline_s=1)
    limits = Limits(
        request_timeout_s=0.02, deadline_s=1, fallback_reserve_s=0.3, planning_timeout_s=0.1
    )
    try:
        result = await asyncio.wait_for(
            Coordinator(store, fixture_planner(), provider, limits).run(run.run_id), 0.5
        )
        assert result.negotiation_outcome == Outcome.FALLBACK
        assert result.elapsed_ms < 500
        assert not provider.all_finished.is_set()
    finally:
        provider.release.set()
        await asyncio.wait_for(provider.all_finished.wait(), 1)
        await asyncio.sleep(0)  # Run completion callbacks that retain late provider usage.
    current = store.get_run(run.run_id)
    assert current == result
    trace = store.export(run.run_id)
    assert len(trace["requests"]) == 3
    assert all(r["late_result"]["output_tokens"] == 50 for r in trace["requests"])
    assert len([e for e in trace["events"] if e["event_type"] == "LATE_PROVIDER_RESPONSE"]) == 3


async def test_event_chain_detects_payload_edit_and_reordering(store):
    provider = FixtureProvider()
    run = start_run(store, provider)
    await Coordinator(store, fixture_planner(), provider).run(run.run_id)
    events = store.events(run.run_id)
    assert verify_event_chain(events)
    assert store.export(run.run_id)["audit_chain_valid"] is True
    assert not verify_event_chain(list(reversed(events)))
    edited = events.copy()
    edited[1] = edited[1].model_copy(update={"payload": {"injected": True}})
    assert not verify_event_chain(edited)


async def test_fallback_outside_catalog_is_non_executable_and_keeps_trigger(store):
    provider = UnavailableProvider()
    run = start_run(store, provider)
    planner = replace(
        fixture_planner(),
        select_fallback=lambda s, c: FallbackResult(
            candidate_id="invented", outcome="FEASIBLE_FALLBACK", reason_code="BAD_SELECTOR"
        ),
    )
    result = await Coordinator(store, planner, provider).run(run.run_id)
    assert result.negotiation_outcome == Outcome.ERROR
    assert result.reason_code == "FALLBACK_OUTSIDE_CATALOG"
    assert result.fallback_reason == "PROVIDER_NOT_CONFIGURED"
    assert result.approval_state == Approval.NONE
    assert result.candidate is None


@pytest.mark.parametrize("status", ["FAIL", "UNKNOWN"])
async def test_invalid_final_candidate_never_becomes_reviewable(store, status):
    provider = FixtureProvider()
    run = start_run(store, provider)

    def invalid_checks(snapshot, plan):
        checks = fixture_checks(snapshot, plan)
        return (checks[0].model_copy(update={"status": status}), *checks[1:])

    planner = replace(fixture_planner(), check_plan=invalid_checks)
    result = await Coordinator(store, planner, provider).run(run.run_id)
    assert result.negotiation_outcome == Outcome.ERROR
    assert result.approval_state == Approval.NONE
    assert result.candidate is None
    rechecks = [e for e in store.events(run.run_id) if e.event_type == "PLAN_REVALIDATED"]
    assert rechecks and any(c["status"] == status for e in rechecks for c in e.payload["checks"])


@pytest.mark.parametrize("defect", ["missing", "duplicate", "wrong_version", "unknown_evidence"])
async def test_incomplete_or_invalid_check_certificates_block_agents(store, defect):
    def broken_catalog(snapshot):
        catalog = fixture_catalog(snapshot)
        entry = catalog.candidates[0]
        checks = entry.checks
        if defect == "missing":
            checks = checks[:-1]
        elif defect == "duplicate":
            checks = (*checks, checks[0])
        elif defect == "wrong_version":
            checks = (checks[0].model_copy(update={"rule_version": "other"}), *checks[1:])
        else:
            checks = (checks[0].model_copy(update={"evidence_ids": ("missing-fact",)}), *checks[1:])
        return catalog.model_copy(
            update={
                "candidates": (entry.model_copy(update={"checks": checks}), *catalog.candidates[1:])
            }
        )

    provider = FixtureProvider()
    run = start_run(store, provider)
    result = await Coordinator(
        store, replace(fixture_planner(), build_catalog=broken_catalog), provider
    ).run(run.run_id)
    assert result.negotiation_outcome == Outcome.ERROR
    assert result.model_call_count == result.agent_invocation_count == 0
    assert result.approval_state == Approval.NONE


async def test_no_plan_is_non_executable(store):
    planner = replace(
        fixture_planner(),
        build_catalog=lambda s: CatalogResult(candidates=(), shortlist_ids=()),
        select_fallback=lambda s, c: FallbackResult(
            candidate_id=None,
            outcome="NO_FEASIBLE_OPTION_IN_SEARCH_SPACE",
            reason_code="ALL_GATES_BLOCKED",
        ),
    )
    provider = FixtureProvider()
    run = start_run(store, provider)
    result = await Coordinator(store, planner, provider).run(run.run_id)
    assert result.negotiation_outcome == Outcome.NO_PLAN
    assert result.candidate is None
    assert result.approval_state == Approval.NONE
    assert result.agent_invocation_count == 0


class WaitingProvider(FixtureProvider):
    def __init__(self):
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.count = 0

    async def generate(self, role, round_number, prompt, timeout_s):
        self.count += 1
        if self.count == 3:
            self.started.set()
        await self.release.wait()
        return await super().generate(role, round_number, prompt, timeout_s)


async def test_cancellation_is_terminal_even_with_pending_role_calls(store):
    provider = WaitingProvider()
    run = start_run(store, provider)
    task = asyncio.create_task(Coordinator(store, fixture_planner(), provider).run(run.run_id))
    await asyncio.wait_for(provider.started.wait(), 1)
    store.cancel(run.run_id)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get_run(run.run_id).negotiation_outcome == Outcome.CANCELLED
    assert store.get_run(run.run_id).candidate is None
    assert provider.count == 3


async def test_state_change_during_negotiation_cannot_fallback_from_old_catalog(store):
    provider = WaitingProvider()
    run = start_run(store, provider)
    task = asyncio.create_task(Coordinator(store, fixture_planner(), provider).run(run.run_id))
    await asyncio.wait_for(provider.started.wait(), 1)
    store.revise_scenario(run.snapshot.model_copy(update={"revision": 2, "resource_revision": 2}))
    provider.release.set()
    result = await task
    assert result.negotiation_outcome == Outcome.INTERRUPTED
    assert result.reason_code == "STATE_CHANGED"
    assert result.candidate is None
    assert not any(e.event_type == "FALLBACK_STARTED" for e in store.events(run.run_id))


async def test_second_round_interruption_removes_proposal_from_executable_result(store):
    class FinalWaitingProvider(WaitingProvider):
        async def generate(self, role, round_number, prompt, timeout_s):
            if round_number == 1:
                return await FixtureProvider.generate(self, role, round_number, prompt, timeout_s)
            return await super().generate(role, round_number, prompt, timeout_s)

    provider = FinalWaitingProvider()
    run = start_run(store, provider)
    task = asyncio.create_task(Coordinator(store, fixture_planner(), provider).run(run.run_id))
    await asyncio.wait_for(provider.started.wait(), 1)
    assert store.get_run(run.run_id).candidate is not None
    store.revise_scenario(run.snapshot.model_copy(update={"revision": 2, "resource_revision": 2}))
    provider.release.set()
    result = await task
    assert result.negotiation_outcome == Outcome.INTERRUPTED
    assert result.candidate is None
    assert result.approval_state == Approval.NONE
    assert result.catalog is not None  # Preserve proposed alternatives for the audit.


async def test_restart_preserves_counters_and_does_not_resubmit(store):
    provider = FixtureProvider()
    run = start_run(store, provider)
    for state in (State.VALIDATING, State.PREPARING):
        store.transition(run.run_id, state)
    store.save_catalog(run.run_id, fixture_catalog(run.snapshot))
    run = store.transition(run.run_id, State.ROUND_1)
    store.begin_request(run.run_id, Role.ATC, 1, build_prompt(run, Role.ATC, 1))
    store.recover_interrupted()
    result = store.get_run(run.run_id)
    assert result.negotiation_outcome == Outcome.INTERRUPTED
    assert result.reason_code == "PROCESS_RESTART"
    assert result.agent_invocation_count == 1
    assert start_run(store, provider).run_id == result.run_id
    assert store.export(run.run_id)["requests"][0]["status"] == "INTERRUPTED"


async def test_late_message_cannot_rewrite_terminal_consensus(store):
    provider = FixtureProvider()
    run = start_run(store, provider)
    result = await Coordinator(store, fixture_planner(), provider).run(run.run_id)
    message = AgentMessage.model_validate(store.export(run.run_id)["messages"][-1])
    changed = message.model_copy(
        update={"payload": message.payload.model_copy(update={"decision": "REJECT"})}
    )
    assert not store.record_message(changed)
    assert store.get_run(run.run_id) == result
    assert store.events(run.run_id)[-1].event_type == "LATE_AGENT_MESSAGE"


async def test_reference_outcomes_do_not_enter_any_role_prompt(store):
    provider = FixtureProvider()
    run = start_run(store, provider)
    await Coordinator(store, fixture_planner(), provider).run(run.run_id)
    result = store.get_run(run.run_id)
    altered = result.model_copy(
        update={
            "snapshot": result.snapshot.model_copy(
                update={
                    "source": result.snapshot.source.model_copy(
                        update={
                            "original_fields": {
                                **result.snapshot.source.original_fields,
                                "ARRIVAL_DELAY": "999999",
                                "ARRIVAL_TIME": "2400",
                                "AIR_TIME": "SECRET_OUTCOME",
                            }
                        }
                    )
                }
            )
        }
    )
    for role in Role:
        assert build_prompt(result, role, 2) == build_prompt(altered, role, 2)


def test_required_rule_set_cannot_be_weakened():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        sample_snapshot().__class__.model_validate(
            {
                **sample_snapshot().model_dump(),
                "required_rule_ids": REQUIRED_RULES[:-1],
            }
        )


def test_expired_snapshot_cannot_start(store):
    provider = FixtureProvider()
    snapshot = sample_snapshot().model_copy(
        update={
            "revision": 2,
            "resource_revision": 2,
            "valid_until": utcnow() - timedelta(seconds=1),
        }
    )
    store.revise_scenario(snapshot)
    from app.contracts import StartRunRequest

    with pytest.raises(ServiceError, match="SNAPSHOT_EXPIRED"):
        store.create_run(
            StartRunRequest(scenario_id=snapshot.scenario_id, scenario_revision=2),
            "expired",
            provider.info,
            "FIXTURE",
            45,
        )
