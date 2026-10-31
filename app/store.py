"""SQLite persistence and atomic review integration for one service worker."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import JsonValue

from app.contracts import (
    AgentMessage,
    AgentOffer,
    AgentVote,
    Approval,
    AuditEvent,
    CandidatePlan,
    CatalogResult,
    ConstraintResult,
    Outcome,
    ProviderInfo,
    ResourceState,
    ReviewHook,
    ReviewRequest,
    Role,
    RunRecord,
    ScenarioSnapshot,
    ServiceError,
    StartRunRequest,
    State,
    canonical_json,
    digest,
    require_passing_checks,
    utcnow,
    verify_event_chain,
)

TRANSITIONS = {
    State.CREATED: {State.VALIDATING},
    State.VALIDATING: {State.PREPARING},
    State.PREPARING: {State.ROUND_1, State.FALLBACK},
    State.ROUND_1: {State.RECONCILING, State.FALLBACK},
    State.RECONCILING: {State.ROUND_2, State.FALLBACK},
    State.ROUND_2: {State.FINAL_VALIDATION, State.REVISING, State.FALLBACK},
    State.REVISING: {State.ROUND_3, State.FALLBACK},
    State.ROUND_3: {State.FINAL_VALIDATION, State.FALLBACK},
    State.FALLBACK: {State.FINAL_VALIDATION},
    State.FINAL_VALIDATION: {State.FALLBACK},
}


def updated(run: RunRecord, **changes: object) -> RunRecord:
    return RunRecord.model_validate({**run.model_dump(), **changes})


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS scenario_revisions (
                    scenario_id TEXT, revision INTEGER, data TEXT NOT NULL,
                    PRIMARY KEY (scenario_id, revision)
                );
                CREATE TABLE IF NOT EXISTS resource_states (
                    resource_id TEXT PRIMARY KEY, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_keys (
                    key TEXT PRIMARY KEY, body_hash TEXT NOT NULL, run_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT, sequence INTEGER, data TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS candidate_plans (
                    run_id TEXT, candidate_id TEXT, data TEXT NOT NULL,
                    PRIMARY KEY (run_id, candidate_id)
                );
                CREATE TABLE IF NOT EXISTS constraint_results (
                    run_id TEXT, candidate_id TEXT, check_id TEXT, data TEXT NOT NULL,
                    PRIMARY KEY (run_id, candidate_id, check_id)
                );
                CREATE TABLE IF NOT EXISTS agent_messages (
                    run_id TEXT, round INTEGER, role TEXT, data TEXT NOT NULL,
                    PRIMARY KEY (run_id, round, role)
                );
                CREATE TABLE IF NOT EXISTS provider_requests (
                    request_id TEXT PRIMARY KEY, run_id TEXT, round INTEGER, role TEXT,
                    data TEXT NOT NULL, UNIQUE (run_id, round, role)
                );
                CREATE TABLE IF NOT EXISTS review_decisions (
                    key TEXT PRIMARY KEY, body_hash TEXT NOT NULL, run_id TEXT NOT NULL,
                    data TEXT NOT NULL
                );
            """)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=1.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _run(self, db: sqlite3.Connection, run_id: str) -> RunRecord:
        row = db.execute("SELECT data FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ServiceError("RUN_NOT_FOUND", 404)
        return RunRecord.model_validate_json(row["data"])

    def _save(self, db: sqlite3.Connection, run: RunRecord) -> None:
        db.execute(
            "INSERT OR REPLACE INTO runs VALUES (?, ?, ?)",
            (run.run_id, run.processing_state, canonical_json(run)),
        )

    def _event(
        self,
        db: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: dict[str, JsonValue] | None = None,
        actor: str = "SYSTEM",
    ) -> None:
        previous = db.execute(
            "SELECT sequence, data FROM events WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        seq = previous["sequence"] + 1 if previous else 1
        previous_hash = json.loads(previous["data"])["event_hash"] if previous else ""
        event = AuditEvent(
            run_id=run_id,
            sequence=seq,
            event_type=event_type,
            actor=actor,
            occurred_at=utcnow(),
            payload=payload or {},
            previous_hash=previous_hash,
            event_hash="",
        )
        event = event.model_copy(
            update={
                "event_hash": digest(event.model_dump(mode="json", exclude={"event_hash"})),
            }
        )
        db.execute("INSERT INTO events VALUES (?, ?, ?)", (run_id, seq, canonical_json(event)))

    def _scenario(
        self,
        db: sqlite3.Connection,
        scenario_id: str,
        revision: int | None = None,
    ) -> ScenarioSnapshot:
        if revision is None:
            row = db.execute(
                "SELECT data FROM scenario_revisions WHERE scenario_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (scenario_id,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT data FROM scenario_revisions WHERE scenario_id=? AND revision=?",
                (scenario_id, revision),
            ).fetchone()
        if row is None:
            raise ServiceError("SCENARIO_NOT_FOUND", 404)
        return ScenarioSnapshot.model_validate_json(row["data"])

    def _resource(self, db: sqlite3.Connection, resource_id: str) -> ResourceState:
        row = db.execute(
            "SELECT data FROM resource_states WHERE resource_id=?", (resource_id,)
        ).fetchone()
        if row is None:
            raise ServiceError("RESOURCE_NOT_FOUND", 404)
        return ResourceState.model_validate_json(row["data"])

    def _require_current(self, db: sqlite3.Connection, snapshot: ScenarioSnapshot) -> None:
        current = self._scenario(db, snapshot.scenario_id)
        resource = self._resource(db, snapshot.resource_id)
        if (
            current.snapshot_hash != snapshot.snapshot_hash
            or resource.revision != snapshot.resource_revision
        ):
            raise ServiceError("STATE_CHANGED")
        if snapshot.expired(utcnow()):
            raise ServiceError("SNAPSHOT_EXPIRED")

    def register_scenario(self, snapshot: ScenarioSnapshot, resource: ResourceState) -> None:
        """Trusted startup import from C; never silently replaces a prior revision."""
        if (snapshot.resource_id, snapshot.resource_revision) != (
            resource.resource_id,
            resource.revision,
        ):
            raise ServiceError("RESOURCE_VERSION_MISMATCH")
        with self.transaction() as db:
            existing = db.execute(
                "SELECT data FROM scenario_revisions WHERE scenario_id=? AND revision=?",
                (snapshot.scenario_id, snapshot.revision),
            ).fetchone()
            if existing and digest(
                ScenarioSnapshot.model_validate_json(existing["data"])
            ) != digest(snapshot):
                raise ServiceError("IMMUTABLE_SCENARIO_REVISION")
            db.execute(
                "INSERT OR IGNORE INTO scenario_revisions VALUES (?, ?, ?)",
                (snapshot.scenario_id, snapshot.revision, canonical_json(snapshot)),
            )
            db.execute(
                "INSERT OR IGNORE INTO resource_states VALUES (?, ?)",
                (resource.resource_id, canonical_json(resource)),
            )

    def list_scenarios(self) -> list[ScenarioSnapshot]:
        with self.transaction() as db:
            ids = db.execute(
                "SELECT DISTINCT scenario_id FROM scenario_revisions ORDER BY scenario_id"
            )
            return [self._scenario(db, row[0]) for row in ids.fetchall()]

    def invalidate_resource(self, resource_id: str) -> None:
        """Retain history but close active/pending decisions on changed source files."""
        with self.transaction() as db:
            self._invalidate(db, resource_id)

    def get_scenario(self, scenario_id: str, revision: int | None = None) -> ScenarioSnapshot:
        with self.transaction() as db:
            return self._scenario(db, scenario_id, revision)

    def get_resource(self, resource_id: str) -> ResourceState:
        with self.transaction() as db:
            return self._resource(db, resource_id)

    def revise_scenario(self, snapshot: ScenarioSnapshot) -> ScenarioSnapshot:
        with self.transaction() as db:
            old = self._scenario(db, snapshot.scenario_id)
            resource = self._resource(db, old.resource_id)
            if snapshot.revision != old.revision + 1 or snapshot.resource_id != old.resource_id:
                raise ServiceError("SCENARIO_REVISION_CONFLICT")
            if snapshot.resource_revision != resource.revision + 1:
                raise ServiceError("RESOURCE_REVISION_CONFLICT")
            db.execute(
                "INSERT INTO scenario_revisions VALUES (?, ?, ?)",
                (snapshot.scenario_id, snapshot.revision, canonical_json(snapshot)),
            )
            resource = resource.model_copy(update={"revision": resource.revision + 1})
            db.execute(
                "UPDATE resource_states SET data=? WHERE resource_id=?",
                (canonical_json(resource), resource.resource_id),
            )
            self._invalidate(db, resource.resource_id)
        return snapshot

    def _invalidate(self, db: sqlite3.Connection, resource_id: str, except_run: str = "") -> None:
        rows = db.execute("SELECT data FROM runs").fetchall()
        for row in rows:
            run = RunRecord.model_validate_json(row["data"])
            if run.snapshot.resource_id != resource_id or run.run_id == except_run:
                continue
            if run.processing_state != State.TERMINAL:
                run = self._terminal(run, Outcome.INTERRUPTED, "STATE_CHANGED")
                self._event(db, run.run_id, "RUN_INTERRUPTED", {"reason_code": "STATE_CHANGED"})
            elif run.approval_state == Approval.PENDING:
                run = updated(run, approval_state=Approval.STALE)
                self._event(
                    db, run.run_id, "RECOMMENDATION_INVALIDATED", {"reason_code": "STATE_CHANGED"}
                )
            else:
                continue
            self._save(db, run)

    def create_run(
        self,
        request: StartRunRequest,
        key: str,
        provider: ProviderInfo,
        planning_mode: str,
        deadline_s: float,
    ) -> tuple[RunRecord, bool]:
        with self.transaction() as db:
            old = db.execute("SELECT * FROM run_keys WHERE key=?", (key,)).fetchone()
            if old:
                if old["body_hash"] != digest(request):
                    raise ServiceError("IDEMPOTENCY_CONFLICT")
                return self._run(db, old["run_id"]), False
            snapshot = self._scenario(db, request.scenario_id, request.scenario_revision)
            self._require_current(db, snapshot)
            if request.parent_run_id:
                parent = self._run(db, request.parent_run_id)
                if parent.snapshot.resource_id != snapshot.resource_id:
                    raise ServiceError("PARENT_RESOURCE_MISMATCH")
            now = utcnow()
            run = RunRecord.model_validate(
                {
                    "run_id": str(uuid4()),
                    "parent_run_id": request.parent_run_id,
                    "snapshot": snapshot,
                    "planning_mode": planning_mode,
                    "provider": provider,
                    "created_at": now,
                    "deadline_at": now + timedelta(seconds=deadline_s),
                }
            )
            self._save(db, run)
            db.execute("INSERT INTO run_keys VALUES (?, ?, ?)", (key, digest(request), run.run_id))
            self._event(
                db,
                run.run_id,
                "RUN_CREATED",
                {
                    "snapshot_hash": snapshot.snapshot_hash,
                    "mode": provider.mode,
                    "planning_mode": planning_mode,
                },
            )
            return run, True

    def existing_run(self, request: StartRunRequest, key: str) -> RunRecord | None:
        """Resolve retries before checking mutable prerequisites for a new run."""
        with self.transaction() as db:
            old = db.execute("SELECT * FROM run_keys WHERE key=?", (key,)).fetchone()
            if old is None:
                return None
            if old["body_hash"] != digest(request):
                raise ServiceError("IDEMPOTENCY_CONFLICT")
            return self._run(db, old["run_id"])

    def get_run(self, run_id: str) -> RunRecord:
        with self.transaction() as db:
            run = self._run(db, run_id)
            if (
                run.approval_state == Approval.PENDING
                and run.expires_at
                and utcnow() >= run.expires_at
            ):
                run = updated(run, approval_state=Approval.EXPIRED)
                self._save(db, run)
                self._event(db, run_id, "RECOMMENDATION_INVALIDATED", {"reason_code": "EXPIRED"})
            return run

    def require_active(self, run_id: str) -> RunRecord:
        with self.transaction() as db:
            run = self._run(db, run_id)
            if run.processing_state == State.TERMINAL:
                raise ServiceError("RUN_TERMINAL")
            self._require_current(db, run.snapshot)
            return run

    def transition(self, run_id: str, state: State, reason: str | None = None) -> RunRecord:
        with self.transaction() as db:
            run = self._run(db, run_id)
            self._require_current(db, run.snapshot)
            if state not in TRANSITIONS.get(run.processing_state, set()):
                raise ServiceError("INVALID_STATE_TRANSITION")
            count = {State.ROUND_1: 1, State.ROUND_2: 2, State.ROUND_3: 3}.get(
                state, run.round_count
            )
            run = updated(
                run,
                processing_state=state,
                round_count=count,
                fallback_reason=reason if state == State.FALLBACK else run.fallback_reason,
            )
            self._save(db, run)
            self._event(db, run_id, "STATE_CHANGED", {"state": state})
            return run

    def event(self, run_id: str, kind: str, payload: dict[str, JsonValue] | None = None) -> None:
        with self.transaction() as db:
            self._run(db, run_id)
            self._event(db, run_id, kind, payload)

    def save_catalog(self, run_id: str, catalog: CatalogResult) -> None:
        with self.transaction() as db:
            run = self._run(db, run_id)
            self._require_current(db, run.snapshot)
            if run.processing_state != State.PREPARING or run.catalog is not None:
                raise ServiceError("CATALOG_ALREADY_FROZEN")
            for candidate in catalog.candidates:
                db.execute(
                    "INSERT INTO candidate_plans VALUES (?, ?, ?)",
                    (run_id, candidate.plan.candidate_id, canonical_json(candidate.plan)),
                )
                for check in candidate.checks:
                    db.execute(
                        "INSERT INTO constraint_results VALUES (?, ?, ?, ?)",
                        (
                            run_id,
                            candidate.plan.candidate_id,
                            check.check_id,
                            canonical_json(check),
                        ),
                    )
            self._save(db, updated(run, catalog=catalog))
            self._event(
                db,
                run_id,
                "CANDIDATES_EVALUATED",
                {
                    "catalog_hash": catalog.catalog_hash,
                    "count": len(catalog.candidates),
                    "shortlist_ids": list(catalog.shortlist_ids),
                },
            )

    def select_proposal(self, run_id: str, plan: CandidatePlan) -> None:
        with self.transaction() as db:
            run = self._run(db, run_id)
            self._require_current(db, run.snapshot)
            if run.processing_state not in (State.RECONCILING, State.REVISING):
                raise ServiceError("INVALID_STATE_TRANSITION")
            if not run.catalog or plan not in [c.plan for c in run.catalog.candidates]:
                raise ServiceError("PLAN_OUTSIDE_CATALOG")
            revising = run.processing_state == State.REVISING
            if revising and (
                run.candidate == plan
                or set(run.votes) != set(Role)
                or not any(v.decision == "REJECT" for v in run.votes.values())
                or any(v.decision == "NEEDS_INFORMATION" for v in run.votes.values())
            ):
                raise ServiceError("INVALID_REVISED_PROPOSAL")
            self._save(
                db,
                updated(
                    run,
                    candidate=plan,
                    votes={},
                    previous_votes=run.votes if revising else {},
                    previous_candidate_id=run.candidate.candidate_id
                    if revising and run.candidate
                    else None,
                ),
            )
            self._event(
                db,
                run_id,
                "PROPOSAL_SELECTED",
                {
                    "candidate_id": plan.candidate_id,
                    "plan_hash": plan.plan_hash,
                    "turn": 3 if revising else 2,
                },
            )

    def begin_request(
        self, run_id: str, role: Role, round_number: int, prompt: dict[str, JsonValue]
    ) -> str:
        with self.transaction() as db:
            run = self._run(db, run_id)
            self._require_current(db, run.snapshot)
            expected = {1: State.ROUND_1, 2: State.ROUND_2, 3: State.ROUND_3}.get(round_number)
            if (
                round_number not in (1, 2, 3)
                or run.processing_state != expected
                or run.agent_invocation_count >= 9
            ):
                raise ServiceError("REQUEST_BUDGET_OR_STATE")
            request_id = str(uuid4())
            request: dict[str, JsonValue] = {
                "request_id": request_id,
                "role": role,
                "round": round_number,
                "started_at": utcnow().isoformat(),
                "status": "STARTED",
                "prompt": prompt,
                "prompt_hash": digest(prompt),
                "model": run.provider.model,
                "mode": run.provider.mode,
                "settings": run.provider.model_dump(mode="json"),
            }
            try:
                db.execute(
                    "INSERT INTO provider_requests VALUES (?, ?, ?, ?, ?)",
                    (request_id, run_id, round_number, role, canonical_json(request)),
                )
            except sqlite3.IntegrityError as error:
                raise ServiceError("DUPLICATE_GENERATION_REQUEST") from error
            run = updated(
                run,
                agent_invocation_count=run.agent_invocation_count + 1,
                model_call_count=run.model_call_count + int(run.provider.mode == "LIVE"),
            )
            self._save(db, run)
            self._event(
                db,
                run_id,
                "AGENT_REQUEST_STARTED",
                {
                    "request_id": request_id,
                    "role": role,
                    "round": round_number,
                },
            )
            return request_id

    def finish_request(
        self, request_id: str, result: dict[str, JsonValue], *, late: bool = False
    ) -> None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM provider_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise ServiceError("REQUEST_NOT_FOUND", 404)
            data = json.loads(row["data"])
            if late:
                data["late_result"] = {**result, "received_at": utcnow().isoformat()}
            elif data["status"] != "STARTED":
                return
            else:
                data.update(result)
                data["finished_at"] = utcnow().isoformat()
            db.execute(
                "UPDATE provider_requests SET data=? WHERE request_id=?",
                (canonical_json(data), request_id),
            )
            self._event(
                db,
                row["run_id"],
                "LATE_PROVIDER_RESPONSE" if late else "AGENT_RESPONSE_RECEIVED",
                {
                    "request_id": request_id,
                    "status": result.get("status"),
                },
            )

    def record_message(self, message: AgentMessage) -> bool:
        with self.transaction() as db:
            run = self._run(db, message.run_id)
            key = (message.run_id, message.round, message.sender_role)
            existing = db.execute(
                "SELECT data FROM agent_messages WHERE run_id=? AND round=? AND role=?", key
            ).fetchone()
            if run.processing_state == State.TERMINAL:
                self._event(
                    db, run.run_id, "LATE_AGENT_MESSAGE", {"message_id": message.message_id}
                )
                return False
            if existing:
                previous = AgentMessage.model_validate_json(existing["data"])
                if previous.payload == message.payload:
                    return False
                raise ServiceError("CONFLICTING_DUPLICATE_MESSAGE")
            self._require_current(db, run.snapshot)
            if (
                message.round != run.round_count
                or not run.catalog
                or message.candidate_set_hash != run.catalog.catalog_hash
                or (
                    message.scenario_revision,
                    message.resource_revision,
                    message.ruleset_version,
                    message.policy_version,
                )
                != (
                    run.snapshot.revision,
                    run.snapshot.resource_revision,
                    run.snapshot.ruleset_version,
                    run.snapshot.policy_version,
                )
            ):
                raise ServiceError("MESSAGE_VERSION_MISMATCH")
            if (
                message.round == 1
                and isinstance(message.payload, AgentOffer)
                and message.message_type == "OFFER"
            ):
                run = updated(run, offers={**run.offers, message.sender_role: message.payload})
            elif (
                message.round in (2, 3)
                and isinstance(message.payload, AgentVote)
                and message.message_type == "VOTE"
            ):
                if message.payload.plan_hash != run.plan_hash:
                    raise ServiceError("VOTE_PLAN_HASH_MISMATCH")
                run = updated(run, votes={**run.votes, message.sender_role: message.payload})
            else:
                raise ServiceError("MESSAGE_TYPE_MISMATCH")
            db.execute(
                "INSERT INTO agent_messages VALUES (?, ?, ?, ?)", (*key, canonical_json(message))
            )
            self._save(db, run)
            self._event(
                db,
                run.run_id,
                "VOTE_RECORDED" if message.round > 1 else "OFFER_RECORDED",
                {
                    "message_id": message.message_id,
                    "role": message.sender_role,
                    "round": message.round,
                },
            )
            return True

    def _terminal(self, run: RunRecord, outcome: Outcome, reason: str | None) -> RunRecord:
        now = utcnow()
        return updated(
            run,
            processing_state=State.TERMINAL,
            negotiation_outcome=outcome,
            reason_code=reason,
            finished_at=now,
            candidate=None,
        )

    def record_elapsed(self, run_id: str, elapsed_ms: int) -> None:
        """Only a live worker can measure monotonic latency; restart latency stays unknown."""
        with self.transaction() as db:
            run = self._run(db, run_id)
            if run.processing_state == State.TERMINAL and run.elapsed_ms is None:
                self._save(db, updated(run, elapsed_ms=elapsed_ms))

    def complete(
        self,
        run_id: str,
        outcome: Outcome,
        reason: str | None = None,
        candidate: CandidatePlan | None = None,
        checks: tuple[ConstraintResult, ...] = (),
        fallback_reason: str | None = None,
        ttl_s: float = 120,
    ) -> RunRecord:
        with self.transaction() as db:
            run = self._run(db, run_id)
            if run.processing_state == State.TERMINAL:
                return run
            feasible = outcome in (Outcome.CONSENSUS, Outcome.FALLBACK)
            if feasible:
                if run.processing_state != State.FINAL_VALIDATION:
                    raise ServiceError("FINAL_VALIDATION_REQUIRED")
                self._require_current(db, run.snapshot)
                if utcnow() >= run.deadline_at:
                    outcome, reason, candidate, checks, feasible = (
                        Outcome.ERROR,
                        "RUN_DEADLINE",
                        None,
                        (),
                        False,
                    )
                elif candidate is None:
                    raise ServiceError("MISSING_PLAN")
                else:
                    require_passing_checks(run.snapshot, candidate, checks)
                    if not run.catalog or not any(
                        c.plan == candidate for c in run.catalog.candidates
                    ):
                        raise ServiceError("PLAN_OUTSIDE_CATALOG")
                    if outcome == Outcome.CONSENSUS and (
                        run.round_count not in (2, 3)
                        or run.agent_invocation_count != 3 * run.round_count
                        or set(run.votes) != set(Role)
                        or set(run.offers) != set(Role)
                        or any(
                            v.decision != "ACCEPT" or v.plan_hash != candidate.plan_hash
                            for v in run.votes.values()
                        )
                    ):
                        raise ServiceError("UNANIMOUS_CURRENT_VOTES_REQUIRED")
            run = self._terminal(run, outcome, reason)
            run = updated(
                run,
                candidate=candidate if feasible else None,
                checks=checks,
                fallback_reason=fallback_reason or run.fallback_reason,
                approval_state=Approval.PENDING if feasible else Approval.NONE,
                expires_at=utcnow() + timedelta(seconds=ttl_s) if feasible else None,
            )
            self._save(db, run)
            kind = (
                "CONSENSUS_REACHED"
                if outcome == Outcome.CONSENSUS
                else ("FALLBACK_SELECTED" if outcome == Outcome.FALLBACK else "ESCALATED")
            )
            self._event(db, run_id, kind, {"outcome": outcome, "reason_code": reason})
            if feasible:
                self._event(db, run_id, "REVIEW_REQUESTED", {"plan_hash": run.plan_hash})
            return run

    def cancel(self, run_id: str) -> RunRecord:
        with self.transaction() as db:
            run = self._run(db, run_id)
            if (
                run.negotiation_outcome == Outcome.CANCELLED
                or run.approval_state == Approval.CANCELLED
            ):
                return run
            if run.processing_state == State.TERMINAL:
                if run.approval_state != Approval.PENDING:
                    raise ServiceError("RUN_ALREADY_TERMINAL")
                run = updated(run, approval_state=Approval.CANCELLED)
            else:
                run = self._terminal(run, Outcome.CANCELLED, "USER_CANCELLED")
                run = updated(run, approval_state=Approval.CANCELLED, candidate=None)
            self._save(db, run)
            self._event(db, run_id, "RUN_CANCELLED")
            return run

    def recover_interrupted(self) -> None:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT data FROM runs WHERE state != ?", (State.TERMINAL,)
            ).fetchall()
            for row in rows:
                run = RunRecord.model_validate_json(row["data"])
                self._save(db, self._terminal(run, Outcome.INTERRUPTED, "PROCESS_RESTART"))
                self._event(db, run.run_id, "RUN_INTERRUPTED", {"reason_code": "PROCESS_RESTART"})
            for row in db.execute("SELECT request_id, data FROM provider_requests").fetchall():
                data = json.loads(row["data"])
                if data["status"] == "STARTED":
                    data.update(status="INTERRUPTED", finished_at=utcnow().isoformat())
                    db.execute(
                        "UPDATE provider_requests SET data=? WHERE request_id=?",
                        (canonical_json(data), row["request_id"]),
                    )

    def review(
        self,
        run_id: str,
        request: ReviewRequest,
        key: str,
        reviewer_id: str,
        hook: ReviewHook,
    ) -> RunRecord:
        """A's hook checks current state and computes changes inside this transaction.

        The hook must be synchronous, pure, and bounded. It must not write to the
        store or perform network I/O. Raising rolls back all changes.
        """
        body_hash = digest(
            {
                "run_id": run_id,
                "reviewer_id": reviewer_id,
                "request": request.model_dump(mode="json"),
            }
        )
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM review_decisions WHERE key=?", (key,)).fetchone()
            if existing:
                if existing["body_hash"] != body_hash:
                    raise ServiceError("IDEMPOTENCY_CONFLICT")
                return RunRecord.model_validate_json(existing["data"])
            run = self._run(db, run_id)
            if run.approval_state != Approval.PENDING or run.candidate is None:
                raise ServiceError("REVIEW_NOT_PENDING")
            self._require_current(db, run.snapshot)
            if run.expires_at is None or utcnow() >= run.expires_at:
                raise ServiceError("RECOMMENDATION_EXPIRED")
            if (
                request.plan_hash,
                request.scenario_revision,
                request.resource_revision,
                request.ruleset_version,
                request.policy_version,
                request.expires_at,
            ) != (
                run.plan_hash,
                run.snapshot.revision,
                run.snapshot.resource_revision,
                run.snapshot.ruleset_version,
                run.snapshot.policy_version,
                run.expires_at,
            ):
                raise ServiceError("REVIEW_BINDING_MISMATCH")
            resource = self._resource(db, run.snapshot.resource_id)
            if request.decision == "APPROVE":
                if run.negotiation_outcome == Outcome.FALLBACK and not request.acknowledge_fallback:
                    raise ServiceError("FALLBACK_ACKNOWLEDGEMENT_REQUIRED", 422)
                effect = hook(run, resource, request)
                require_passing_checks(run.snapshot, run.candidate, effect.checks)
                self._require_current(db, run.snapshot)
                if utcnow() >= run.expires_at:
                    raise ServiceError("RECOMMENDATION_EXPIRED")
                if effect.resource_data is None:
                    raise ServiceError("MISSING_RESOURCE_UPDATE")
                resource = ResourceState(
                    resource_id=resource.resource_id,
                    revision=resource.revision + 1,
                    data=effect.resource_data,
                )
                db.execute(
                    "UPDATE resource_states SET data=? WHERE resource_id=?",
                    (canonical_json(resource), resource.resource_id),
                )
                run = updated(run, approval_state=Approval.APPLIED, checks=effect.checks)
                self._event(
                    db,
                    run_id,
                    "APPROVAL_REVALIDATED",
                    {
                        "plan_hash": run.plan_hash,
                        "checks": [check.model_dump(mode="json") for check in effect.checks],
                    },
                    actor=reviewer_id,
                )
                self._invalidate(db, resource.resource_id, except_run=run_id)
                kind = "SIMULATION_APPLIED"
            else:
                run = updated(run, approval_state=Approval.REJECTED)
                kind = "REVIEW_REJECTED"
            self._save(db, run)
            self._event(
                db,
                run_id,
                kind,
                {
                    "reason": request.reason,
                    "plan_hash": run.plan_hash,
                    "resource_revision": resource.revision,
                },
                actor=reviewer_id,
            )
            db.execute(
                "INSERT INTO review_decisions VALUES (?, ?, ?, ?)",
                (key, body_hash, run_id, canonical_json(run)),
            )
            return run

    def events(self, run_id: str, after_seq: int = 0) -> list[AuditEvent]:
        with self.transaction() as db:
            self._run(db, run_id)
            rows = db.execute(
                "SELECT data FROM events WHERE run_id=? AND sequence>? ORDER BY sequence",
                (run_id, after_seq),
            ).fetchall()
            return [AuditEvent.model_validate_json(row[0]) for row in rows]

    def export(self, run_id: str) -> dict[str, JsonValue]:
        self.get_run(run_id)  # Commit any wall-clock expiry before taking a consistent snapshot.
        with self.transaction() as db:
            run = self._run(db, run_id)
            events = [
                AuditEvent.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT data FROM events WHERE run_id=? ORDER BY sequence", (run_id,)
                ).fetchall()
            ]
            requests = db.execute(
                "SELECT data FROM provider_requests WHERE run_id=? ORDER BY round, role",
                (run_id,),
            ).fetchall()
            messages = db.execute(
                "SELECT data FROM agent_messages WHERE run_id=? ORDER BY round, role",
                (run_id,),
            ).fetchall()
        return {
            "schema_version": "1",
            "run": cast(dict[str, JsonValue], run.model_dump(mode="json")),
            "plan_hash": run.plan_hash,
            "audit_chain_valid": verify_event_chain(events),
            "events": [cast(JsonValue, e.model_dump(mode="json")) for e in events],
            "requests": [json.loads(row[0]) for row in requests],
            "messages": [json.loads(row[0]) for row in messages],
        }
