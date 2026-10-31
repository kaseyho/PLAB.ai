"""Human review gate and simulated application (PRD 11.1-11.5, FR-11 to FR-13).

Agent consensus never authorises anything. A trusted reviewer approves or rejects the
persisted plan; approval re-runs every hard check against the *current* simulator state
and applies the plan in the same transaction, so a stale or competing approval cannot
slip in between check and update.

Storage mechanics belong to Developer 2. This module only needs the `ReviewStore`
protocol below; Developer 2's SQLite store implements it and mounts `review` behind
`POST /api/runs/{id}/decision`.
"""

import hashlib
import hmac
import json
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.constraints import CandidatePlan, check_plan, is_approval_ready
from app.scenarios import ScenarioSnapshot, snapshot_from_dict

RECOMMENDATION_TTL = timedelta(seconds=120)  # demo UI validity policy, not an aviation standard

NegotiationOutcome = Literal[
    "FEASIBLE_CONSENSUS", "FEASIBLE_FALLBACK", "NO_FEASIBLE_OPTION_IN_SEARCH_SPACE",
    "INSUFFICIENT_DATA", "SYSTEM_ERROR", "CANCELLED", "INTERRUPTED",
]
ApprovalState = Literal[
    "NOT_APPLICABLE", "PENDING", "APPLIED_SIMULATION", "REJECTED", "STALE", "EXPIRED", "CANCELLED",
]
Decision = Literal["APPROVE", "REJECT", "CANCEL"]
ReviewStatus = Literal[
    "APPLIED_SIMULATION", "REJECTED", "CANCELLED", "STALE", "EXPIRED",
    "UNAUTHORIZED", "INVALID_REQUEST", "VERSION_CONFLICT", "IDEMPOTENCY_CONFLICT",
    "NOT_REVIEWABLE", "ALREADY_TERMINAL", "NOT_FOUND",
]
REVIEWABLE_OUTCOMES = ("FEASIBLE_CONSENSUS", "FEASIBLE_FALLBACK")
HTTP_STATUS: dict[str, int] = {
    "APPLIED_SIMULATION": 200, "REJECTED": 200, "CANCELLED": 200,
    "STALE": 409, "EXPIRED": 409, "VERSION_CONFLICT": 409, "IDEMPOTENCY_CONFLICT": 409,
    "NOT_REVIEWABLE": 409, "ALREADY_TERMINAL": 409,
    "UNAUTHORIZED": 403, "INVALID_REQUEST": 422, "NOT_FOUND": 404,
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExpectedVersions(_Frozen):
    """What the reviewer saw on screen. Must match the persisted recommendation exactly."""

    plan_hash: str
    scenario_revision: int
    resource_revision: int
    ruleset_version: str
    policy_version: str


class ReviewRequest(_Frozen):
    run_id: str = Field(min_length=1, max_length=100)
    decision: Decision
    expected: ExpectedVersions
    idempotency_key: str = Field(min_length=8, max_length=100)
    reason: Optional[str] = Field(default=None, max_length=500)
    acknowledge_no_consensus: bool = False


class ReviewerSession(_Frozen):
    """Server-side trusted session. Never built from request bodies or model output."""

    reviewer_id: str
    token: str


class Recommendation(_Frozen):
    """The persisted terminal run as Developer 2 stores it; only the fields review needs."""

    run_id: str
    scenario_id: str
    negotiation_outcome: NegotiationOutcome
    approval_state: ApprovalState
    plan: Optional[CandidatePlan]
    expires_at: datetime


class ReviewResult(_Frozen):
    status: ReviewStatus
    http_status: int
    run_id: str
    message: str
    resource_revision: Optional[int] = None
    failed_rules: list[str] = []
    reviewer_id: Optional[str] = None
    decided_at: Optional[datetime] = None


class ReviewTransaction(Protocol):
    def get_recommendation(self, run_id: str) -> Optional[Recommendation]: ...
    def get_resource_state(self, scenario_id: str) -> ScenarioSnapshot: ...
    def get_idempotent(self, key: str) -> Optional[tuple[str, ReviewResult]]: ...
    def put_idempotent(self, key: str, fingerprint: str, result: ReviewResult) -> None: ...
    def set_approval_state(self, run_id: str, state: ApprovalState) -> None: ...
    def put_resource_state(self, snapshot: ScenarioSnapshot) -> None: ...
    def append_event(self, run_id: str, event_type: str, payload: dict) -> None: ...


class ReviewStore(Protocol):
    def transaction(self) -> AbstractContextManager[ReviewTransaction]:
        """One exclusive write transaction: commit on normal exit, roll back on exception."""
        ...


def new_recommendation(
    run_id: str, scenario_id: str, outcome: NegotiationOutcome, plan: Optional[CandidatePlan], now: datetime,
) -> Recommendation:
    """How the coordinator should persist a terminal run so it can be reviewed."""
    reviewable = outcome in REVIEWABLE_OUTCOMES and plan is not None
    return Recommendation(
        run_id=run_id, scenario_id=scenario_id, negotiation_outcome=outcome,
        approval_state="PENDING" if reviewable else "NOT_APPLICABLE",
        plan=plan, expires_at=now + RECOMMENDATION_TTL,
    )


def apply_plan(snapshot: ScenarioSnapshot, plan: CandidatePlan) -> ScenarioSnapshot:
    """Reserve the runway slot and gate interval, and bump the resource revision."""
    raw = snapshot.model_dump(mode="json")
    overlay = raw["overlay"]
    overlay["movements"].append({
        "movement_id": f"APPLIED-{plan.candidate_id}",
        "time_s": plan.landing_time_s,
        "wake_category": overlay["aircraft"]["wake_category"],
        "occupancy_s": overlay["runway"]["focal_occupancy_s"],
    })
    buffer = overlay["service"]["gate_buffer_s"]
    gate = next(g for g in overlay["gates"] if g["gate_id"] == plan.gate_id)
    gate["reservations"].append([plan.gate_in_time_s - buffer, plan.gate_release_s + buffer])
    raw["resource_revision"] = snapshot.resource_revision + 1
    raw.pop("snapshot_hash")
    return snapshot_from_dict(raw)


def _fingerprint(request: ReviewRequest, reviewer_id: str) -> str:
    body = {**request.model_dump(mode="json"), "reviewer_id": reviewer_id}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _authorized(session: Optional[ReviewerSession], presented_token: Optional[str]) -> bool:
    return bool(session and presented_token and hmac.compare_digest(session.token, presented_token))


def review(
    store: ReviewStore,
    session: Optional[ReviewerSession],
    presented_token: Optional[str],
    payload: dict,
    now: Optional[datetime] = None,
) -> ReviewResult:
    """Approve, reject or cancel one persisted recommendation (PRD 11.3 procedure)."""
    now = now or datetime.now(timezone.utc)
    run_id = str(payload.get("run_id", "")) if isinstance(payload, dict) else ""

    # 1. Trusted reviewer session first; nothing is read or written for an unauthorised caller.
    if not _authorized(session, presented_token):
        return _result("UNAUTHORIZED", run_id, "A trusted reviewer session is required.")
    # 2. Strict request schema.
    try:
        request = ReviewRequest.model_validate(payload)
    except ValidationError as error:
        return _result("INVALID_REQUEST", run_id, f"Invalid review request: {error.error_count()} error(s).")
    if request.decision == "REJECT" and not (request.reason and request.reason.strip()):
        return _result("INVALID_REQUEST", run_id, "A rejection needs a short reason.")

    fingerprint = _fingerprint(request, session.reviewer_id)
    # 3. Everything below runs in one exclusive transaction.
    with store.transaction() as tx:
        previous = tx.get_idempotent(request.idempotency_key)
        if previous is not None:
            stored_fingerprint, stored_result = previous
            if stored_fingerprint == fingerprint:
                return stored_result
            return _result("IDEMPOTENCY_CONFLICT", run_id, "This idempotency key was already used for a different request.")
        result = _decide(tx, request, session.reviewer_id, now)
        if result.status != "NOT_FOUND":
            tx.put_idempotent(request.idempotency_key, fingerprint, result)
        return result


def _decide(tx: ReviewTransaction, request: ReviewRequest, reviewer_id: str, now: datetime) -> ReviewResult:
    run_id = request.run_id
    rec = tx.get_recommendation(run_id)
    if rec is None:
        return _result("NOT_FOUND", run_id, "No such run.")
    if rec.negotiation_outcome not in REVIEWABLE_OUTCOMES or rec.plan is None:
        return _result("NOT_REVIEWABLE", run_id,
                       f"{rec.negotiation_outcome} has no executable plan; it needs qualified review outside the simulator.")
    if rec.approval_state != "PENDING":
        return _result("ALREADY_TERMINAL", run_id, f"Recommendation is already {rec.approval_state}.")

    plan = rec.plan
    persisted = ExpectedVersions(
        plan_hash=plan.plan_hash, scenario_revision=plan.scenario_revision,
        resource_revision=plan.resource_revision, ruleset_version=plan.ruleset_version,
        policy_version=plan.policy_version,
    )
    if request.expected != persisted:
        return _result("VERSION_CONFLICT", run_id, "The plan on screen differs from the stored plan; reload before deciding.")

    audit = {"reviewer_id": reviewer_id, "decision": request.decision, "plan_hash": plan.plan_hash,
             "reason": request.reason, "decided_at": now.isoformat()}
    if request.decision == "REJECT":
        tx.set_approval_state(run_id, "REJECTED")
        tx.append_event(run_id, "REVIEW_REJECTED", audit)
        return _result("REJECTED", run_id, "Rejected. The simulated schedule is unchanged.", reviewer_id=reviewer_id, decided_at=now)
    if request.decision == "CANCEL":
        tx.set_approval_state(run_id, "CANCELLED")
        tx.append_event(run_id, "RUN_CANCELLED", audit)
        return _result("CANCELLED", run_id, "Cancelled. The simulated schedule is unchanged.", reviewer_id=reviewer_id, decided_at=now)

    # APPROVE
    if rec.negotiation_outcome == "FEASIBLE_FALLBACK" and not request.acknowledge_no_consensus:
        return _result("INVALID_REQUEST", run_id, "This is a fallback plan without agent consensus; acknowledge that before approving.")
    if now >= rec.expires_at:
        tx.set_approval_state(run_id, "EXPIRED")
        tx.append_event(run_id, "RECOMMENDATION_INVALIDATED", {**audit, "cause": "EXPIRED"})
        return _result("EXPIRED", run_id, "The recommendation expired before approval. Start a new run.")

    current = tx.get_resource_state(rec.scenario_id)
    if current.resource_revision != plan.resource_revision:
        tx.set_approval_state(run_id, "STALE")
        tx.append_event(run_id, "RECOMMENDATION_INVALIDATED",
                        {**audit, "cause": "RESOURCE_CHANGED", "current_resource_revision": current.resource_revision})
        return _result("STALE", run_id, "Airport resources changed since this plan was made. Start a new run.",
                       resource_revision=current.resource_revision)

    # Re-run every hard rule against the current state, inside this transaction.
    checks = check_plan(current, plan)
    if not is_approval_ready(checks):
        failed = [c.rule_id for c in checks if c.status != "PASS"]
        tx.set_approval_state(run_id, "STALE")
        tx.append_event(run_id, "RECOMMENDATION_INVALIDATED", {**audit, "cause": "REVALIDATION_FAILED", "failed_rules": failed})
        return _result("STALE", run_id, "The plan no longer passes every hard check. Start a new run.",
                       resource_revision=current.resource_revision, failed_rules=failed)

    applied = apply_plan(current, plan)
    tx.put_resource_state(applied)
    tx.set_approval_state(run_id, "APPLIED_SIMULATION")
    tx.append_event(run_id, "SIMULATION_APPLIED", {**audit, "resource_revision": applied.resource_revision})
    return _result("APPLIED_SIMULATION", run_id, "Applied to the simulation only.",
                   resource_revision=applied.resource_revision, reviewer_id=reviewer_id, decided_at=now)


def _result(status: ReviewStatus, run_id: str, message: str, **fields) -> ReviewResult:
    return ReviewResult(status=status, http_status=HTTP_STATUS[status], run_id=run_id, message=message, **fields)
