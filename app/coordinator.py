"""A finite two-round protocol. Rules and application authority remain outside agents."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TypeVar

from pydantic import JsonValue, ValidationError

from app.agents import AgentProvider, ProviderReply, build_prompt, shortlist, validate_payload
from app.contracts import (
    AgentMessage,
    AgentOffer,
    AgentVote,
    CandidatePlan,
    ConstraintResult,
    Outcome,
    PlanningHooks,
    Role,
    RunRecord,
    ServiceError,
    State,
    require_passing_checks,
    utcnow,
)
from app.store import Store

T = TypeVar("T")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limits:
    request_timeout_s: float = 12
    deadline_s: float = 45
    fallback_reserve_s: float = 5
    planning_timeout_s: float = 2
    recommendation_ttl_s: float = 120

    def __post_init__(self) -> None:
        if not (
            0 < self.request_timeout_s <= 12
            and 0 < self.deadline_s <= 45
            and 0 < self.fallback_reserve_s < self.deadline_s
            and 0 < self.planning_timeout_s <= self.fallback_reserve_s
            and 0 < self.recommendation_ttl_s <= 120
        ):
            raise ValueError("Limits must stay inside the PRD budgets")


class Coordinator:
    def __init__(
        self,
        store: Store,
        planner: PlanningHooks,
        provider: AgentProvider,
        limits: Limits | None = None,
    ) -> None:
        self.store, self.planner, self.provider = store, planner, provider
        self.limits = limits or Limits()

    async def run(self, run_id: str, accepted_at: float | None = None) -> RunRecord:
        start = time.monotonic() if accepted_at is None else accepted_at
        run = self.store.get_run(run_id)
        remaining = min(
            self.limits.deadline_s - (time.monotonic() - start),
            (run.deadline_at - utcnow()).total_seconds(),
        )
        try:
            async with asyncio.timeout(max(0, remaining)):
                await self._drive(
                    run_id, time.monotonic() + remaining - self.limits.fallback_reserve_s
                )
        except asyncio.CancelledError:
            # The API persists cancellation first. Shutdown interrupts rather than applies.
            self.store.complete(run_id, Outcome.INTERRUPTED, "WORKER_CANCELLED")
            raise
        except TimeoutError:
            self.store.complete(run_id, Outcome.ERROR, "RUN_DEADLINE")
        except ServiceError as error:
            outcome = (
                Outcome.INTERRUPTED
                if error.code in ("STATE_CHANGED", "SNAPSHOT_EXPIRED")
                else Outcome.ERROR
            )
            self.store.complete(run_id, outcome, error.code)
        except Exception as error:
            # Never expose exception strings: provider errors can include sensitive content.
            self.store.event(run_id, "SYSTEM_FAILURE", {"error_type": type(error).__name__})
            self.store.complete(run_id, Outcome.ERROR, "INTERNAL_ERROR")
        finally:
            self.store.record_elapsed(run_id, max(0, int((time.monotonic() - start) * 1000)))
        return self.store.get_run(run_id)

    async def _planning(self, function: Callable[[], T]) -> T:
        # C supplies pure bounded functions. A timed-out thread cannot write run state.
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(function), self.limits.planning_timeout_s
            )
        except TimeoutError as error:
            raise ServiceError("PLANNING_TIMEOUT") from error

    async def _drive(self, run_id: str, negotiation_deadline: float) -> None:
        run = self.store.transition(run_id, State.VALIDATING)
        if not run.snapshot.source.qualifies():
            self.store.event(run_id, "INPUT_REJECTED", {"reason_code": "SOURCE_NOT_ELIGIBLE"})
            self.store.complete(run_id, Outcome.INSUFFICIENT, "SOURCE_NOT_ELIGIBLE")
            return
        missing = run.snapshot.missing_inputs()
        if missing:
            self.store.event(run_id, "INPUT_REJECTED", {"missing_fact_ids": list(missing)})
            self.store.complete(run_id, Outcome.INSUFFICIENT, "MISSING_REQUIRED_INPUTS")
            return
        self.store.event(run_id, "INPUT_VALIDATED")
        self.store.transition(run_id, State.PREPARING)
        catalog = await self._planning(lambda: self.planner.build_catalog(run.snapshot))
        self.store.require_active(run_id)
        self.store.save_catalog(run_id, catalog)
        for entry in catalog.candidates:
            if entry.plan.candidate_id in catalog.shortlist_ids:
                require_passing_checks(run.snapshot, entry.plan, entry.checks)
        if not catalog.shortlist_ids:
            await self._fallback(run_id, "EMPTY_FEASIBLE_SHORTLIST")
            return
        if self.provider.info.mode == "UNAVAILABLE":
            await self._fallback(run_id, "PROVIDER_NOT_CONFIGURED")
            return
        try:
            self.store.transition(run_id, State.ROUND_1)
            await self._round(run_id, 1, negotiation_deadline)
            run = self.store.transition(run_id, State.RECONCILING)
            # An objection is a request to recheck, never permission to waive a rule.
            for candidate_id in {
                o.candidate_id for offer in run.offers.values() for o in offer.objections
            }:
                plan = next(p for p in shortlist(run) if p.candidate_id == candidate_id)
                await self._check(run, plan)
            proposal = self._reconcile(run)
            if proposal is None:
                raise ServiceError("NO_MUTUAL_OFFER")
            self.store.select_proposal(run_id, proposal)
            self.store.transition(run_id, State.ROUND_2)
            await self._round(run_id, 2, negotiation_deadline)
            run = self.store.require_active(run_id)
            if set(run.votes) != set(Role) or any(
                v.decision != "ACCEPT" for v in run.votes.values()
            ):
                raise ServiceError("FINAL_VOTE_DISSENT")
            self.store.transition(run_id, State.FINAL_VALIDATION)
            checks = await self._check(run, proposal)
            self.store.complete(
                run_id,
                Outcome.CONSENSUS,
                candidate=proposal,
                checks=checks,
                ttl_s=self.limits.recommendation_ttl_s,
            )
        except ServiceError as error:
            if error.code in ("STATE_CHANGED", "SNAPSHOT_EXPIRED", "RUN_TERMINAL"):
                raise
            await self._fallback(run_id, error.code)

    @staticmethod
    def _reconcile(run: RunRecord) -> CandidatePlan | None:
        if set(run.offers) != set(Role):
            raise ServiceError("MISSING_ROLE_OFFER")
        plans = shortlist(run)
        initial = {p.candidate_id for p in plans}
        flexible = initial.copy()
        for offer in run.offers.values():
            initial.intersection_update(offer.acceptable_candidate_ids)
            flexible.intersection_update(
                set(offer.acceptable_candidate_ids) | set(offer.concession_candidate_ids)
            )
        common = initial or flexible
        return min(
            (p for p in plans if p.candidate_id in common),
            key=lambda p: p.joint_order(),
            default=None,
        )

    async def _check(self, run: RunRecord, plan: CandidatePlan) -> tuple[ConstraintResult, ...]:
        checks = await self._planning(partial(self.planner.check_plan, run.snapshot, plan))
        self.store.require_active(run.run_id)
        # Retain failed and unknown rechecks too; otherwise a final rejection is unauditable.
        self.store.event(
            run.run_id,
            "PLAN_REVALIDATED",
            {
                "candidate_id": plan.candidate_id,
                "plan_hash": plan.plan_hash,
                "checks": [check.model_dump(mode="json") for check in checks],
            },
        )
        require_passing_checks(run.snapshot, plan, checks)
        return checks

    async def _fallback(self, run_id: str, reason: str) -> None:
        run = self.store.require_active(run_id)
        if run.catalog is None:
            raise ServiceError("CATALOG_MISSING")
        self.store.transition(run_id, State.FALLBACK, reason)
        self.store.event(run_id, "FALLBACK_STARTED", {"reason_code": reason})
        catalog = run.catalog
        fallback = await self._planning(lambda: self.planner.select_fallback(run.snapshot, catalog))
        self.store.require_active(run_id)
        if fallback.outcome != Outcome.FALLBACK.value:
            if fallback.candidate_id is not None:
                raise ServiceError("NONEXECUTABLE_FALLBACK_HAS_PLAN")
            self.store.complete(
                run_id, Outcome(fallback.outcome), fallback.reason_code, fallback_reason=reason
            )
            return
        entry = next(
            (c for c in catalog.candidates if c.plan.candidate_id == fallback.candidate_id), None
        )
        if entry is None:
            raise ServiceError("FALLBACK_OUTSIDE_CATALOG")
        self.store.transition(run_id, State.FINAL_VALIDATION)
        checks = await self._check(run, entry.plan)
        self.store.complete(
            run_id,
            Outcome.FALLBACK,
            fallback.reason_code,
            entry.plan,
            checks,
            fallback_reason=reason,
            ttl_s=self.limits.recommendation_ttl_s,
        )

    async def _round(self, run_id: str, number: int, deadline: float) -> None:
        tasks = [asyncio.create_task(self._invoke(run_id, role, number, deadline)) for role in Role]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            failure = next(
                (task.exception() for task in tasks if task in done and task.exception()), None
            )
            if failure:
                raise failure
            if pending:
                await asyncio.gather(*pending)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _reply_data(reply: ProviderReply) -> dict[str, JsonValue]:
        return {
            "status": "RECEIVED",
            "raw_output": reply.text[:64000],
            "provider_status": reply.status,
            "response_id": reply.response_id,
            "actual_model": reply.actual_model,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
        }

    def _late_reply(self, request_id: str, task: asyncio.Task[ProviderReply]) -> None:
        if task.cancelled():
            return
        try:
            result: dict[str, JsonValue] = (
                {"status": "ERROR", "error_type": type(task.exception()).__name__}
                if task.exception()
                else self._reply_data(task.result())
            )
            self.store.finish_request(request_id, result, late=True)
        except Exception as error:
            logger.error("Late-response persistence failed: %s", type(error).__name__)

    async def _invoke(self, run_id: str, role: Role, number: int, deadline: float) -> None:
        timeout = min(self.limits.request_timeout_s, deadline - time.monotonic())
        if timeout <= 0:
            raise ServiceError("NEGOTIATION_DEADLINE")
        run = self.store.require_active(run_id)
        prompt = build_prompt(run, role, number)
        request_id = self.store.begin_request(run_id, role, number, prompt)
        task = asyncio.create_task(self.provider.generate(role, number, prompt, timeout))
        received = False
        try:
            # wait_for waits for cancellation acknowledgement. A provider that ignores
            # cancellation must not hold the run open or consume the fallback reserve.
            done, _ = await asyncio.wait((task,), timeout=timeout)
            if not done:
                raise TimeoutError
            received = True
            reply = task.result()
            self.store.finish_request(request_id, self._reply_data(reply))
            self.store.require_active(run_id)
            if reply.status != "completed" or not reply.text:
                raise ServiceError("PROVIDER_INCOMPLETE_OR_REFUSED")
            if len(reply.text) > 64000:
                raise ServiceError("PROVIDER_OUTPUT_TOO_LARGE")
            payload = (
                AgentOffer.model_validate_json(reply.text)
                if number == 1
                else AgentVote.model_validate_json(reply.text)
            )
            validate_payload(run, role, payload)
            message = AgentMessage(
                message_id=request_id,
                run_id=run_id,
                sender_role=role,
                round=1 if number == 1 else 2,
                message_type="OFFER" if number == 1 else "VOTE",
                scenario_revision=run.snapshot.revision,
                resource_revision=run.snapshot.resource_revision,
                ruleset_version=run.snapshot.ruleset_version,
                policy_version=run.snapshot.policy_version,
                candidate_set_hash=run.catalog.catalog_hash if run.catalog else "",
                payload=payload,
                received_at=utcnow(),
            )
            self.store.record_message(message)
        except asyncio.CancelledError:
            self.store.finish_request(request_id, {"status": "CANCELLED"})
            raise
        except TimeoutError as error:
            self.store.finish_request(request_id, {"status": "TIMEOUT"})
            raise ServiceError("AGENT_TIMEOUT") from error
        except ValidationError as error:
            self.store.event(
                run_id, "AGENT_PROTOCOL_ERROR", {"role": role, "reason_code": "INVALID_SCHEMA"}
            )
            raise ServiceError("INVALID_AGENT_SCHEMA") from error
        except ServiceError as error:
            self.store.event(
                run_id, "AGENT_PROTOCOL_ERROR", {"role": role, "reason_code": error.code}
            )
            raise
        except Exception as error:
            self.store.finish_request(
                request_id, {"status": "ERROR", "error_type": type(error).__name__}
            )
            raise ServiceError("PROVIDER_ERROR") from error
        finally:
            if not received:
                task.cancel()
                task.add_done_callback(partial(self._late_reply, request_id))
