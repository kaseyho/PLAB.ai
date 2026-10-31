"""Human review gate tests (PRD 14.7 items 17-22). Uses an in-memory stand-in for Developer 2's store."""

import copy
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.approvals import ReviewerSession, new_recommendation, review
from app.constraints import derive_plan
from tests.conftest import scenario

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
SESSION = ReviewerSession(reviewer_id="duty-manager-demo", token="local-demo-token")
TOKEN = "local-demo-token"


class MemoryStore:
    """Minimal ReviewStore: one lock, copy-on-begin, swap-on-commit, discard on exception."""

    def __init__(self, snapshot, recommendations):
        self.state = {"resources": {snapshot.scenario_id: snapshot},
                      "recommendations": {r.run_id: r for r in recommendations},
                      "idempotency": {}, "events": []}
        self._lock = threading.Lock()
        self.fail_on_resource_write = False

    @contextmanager
    def transaction(self):
        with self._lock:
            working = copy.deepcopy(self.state)
            yield _Tx(working, self)
            self.state = working

    def resources(self, scenario_id="D01"):
        return self.state["resources"][scenario_id]

    def approval_state(self, run_id):
        return self.state["recommendations"][run_id].approval_state

    def event_types(self, run_id):
        return [e[1] for e in self.state["events"] if e[0] == run_id]


class _Tx:
    def __init__(self, state, store):
        self.s, self.store = state, store

    def get_recommendation(self, run_id):
        return self.s["recommendations"].get(run_id)

    def get_resource_state(self, scenario_id):
        return self.s["resources"][scenario_id]

    def get_idempotent(self, key):
        return self.s["idempotency"].get(key)

    def put_idempotent(self, key, fingerprint, result):
        self.s["idempotency"][key] = (fingerprint, result)

    def set_approval_state(self, run_id, state):
        rec = self.s["recommendations"][run_id]
        self.s["recommendations"][run_id] = rec.model_copy(update={"approval_state": state})

    def put_resource_state(self, snapshot):
        if self.store.fail_on_resource_write:
            raise OSError("disk full")
        self.s["resources"][snapshot.scenario_id] = snapshot

    def append_event(self, run_id, event_type, payload):
        self.s["events"].append((run_id, event_type, payload))


def make_store(*runs):
    """runs: (run_id, outcome, landing, route) on the D01 fixture."""
    snapshot = scenario()
    recs = [new_recommendation(run_id, "D01", outcome,
                               derive_plan(snapshot, landing, route) if landing else None, NOW)
            for run_id, outcome, landing, route in runs]
    return MemoryStore(snapshot, recs)


def request(store, run_id, decision="APPROVE", key=None, **extra):
    plan = store.state["recommendations"][run_id].plan
    expected = {"plan_hash": plan.plan_hash, "scenario_revision": plan.scenario_revision,
                "resource_revision": plan.resource_revision, "ruleset_version": plan.ruleset_version,
                "policy_version": plan.policy_version}
    return {"run_id": run_id, "decision": decision, "expected": expected,
            "idempotency_key": key or f"key-{run_id}-{decision}", **extra}


CONSENSUS_C2 = ("run-1", "FEASIBLE_CONSENSUS", 960, "G1-long")


def test_nothing_changes_before_approval():
    store = make_store(CONSENSUS_C2)
    assert store.approval_state("run-1") == "PENDING"
    assert store.resources().resource_revision == 1


def test_valid_approval_applies_exactly_the_plan_once():
    store = make_store(CONSENSUS_C2)
    result = review(store, SESSION, TOKEN, request(store, "run-1"), NOW)
    assert (result.status, result.http_status, result.resource_revision) == ("APPLIED_SIMULATION", 200, 2)
    after = store.resources()
    assert after.overlay.gate("G1").reservations[-1] == (1500, 2700)  # C2 gate-in 1500, release 2700
    assert after.overlay.movements[-1].time_s == 960
    assert store.approval_state("run-1") == "APPLIED_SIMULATION"
    assert store.event_types("run-1") == ["SIMULATION_APPLIED"]


@pytest.mark.parametrize("session, token", [
    (SESSION, None), (SESSION, "wrong-token"), (None, TOKEN),
])
def test_missing_or_wrong_reviewer_authority_cannot_change_anything(session, token):
    store = make_store(CONSENSUS_C2)
    result = review(store, session, token, request(store, "run-1"), NOW)
    assert (result.status, result.http_status) == ("UNAUTHORIZED", 403)
    assert store.resources().resource_revision == 1
    assert store.approval_state("run-1") == "PENDING"


def test_duplicate_approval_with_same_key_returns_original_result_and_applies_once():
    store = make_store(CONSENSUS_C2)
    first = review(store, SESSION, TOKEN, request(store, "run-1"), NOW)
    again = review(store, SESSION, TOKEN, request(store, "run-1"), NOW + timedelta(seconds=5))
    assert again == first
    assert store.resources().resource_revision == 2
    assert store.event_types("run-1") == ["SIMULATION_APPLIED"]


def test_same_key_with_different_body_is_a_conflict():
    store = make_store(CONSENSUS_C2)
    review(store, SESSION, TOKEN, request(store, "run-1", key="shared-key-1"), NOW)
    clash = review(store, SESSION, TOKEN, request(store, "run-1", "REJECT", key="shared-key-1", reason="x"), NOW)
    assert (clash.status, clash.http_status) == ("IDEMPOTENCY_CONFLICT", 409)


def test_second_key_cannot_apply_an_applied_run_again():
    store = make_store(CONSENSUS_C2)
    review(store, SESSION, TOKEN, request(store, "run-1", key="first-key-1"), NOW)
    second = review(store, SESSION, TOKEN, request(store, "run-1", key="second-key"), NOW)
    assert (second.status, second.http_status) == ("ALREADY_TERMINAL", 409)
    assert store.resources().resource_revision == 2


def test_reject_leaves_schedule_unchanged_and_blocks_later_approval():
    store = make_store(CONSENSUS_C2)
    rejected = review(store, SESSION, TOKEN, request(store, "run-1", "REJECT", reason="Prefer to wait for G2 crew"), NOW)
    assert rejected.status == "REJECTED"
    assert store.resources().resource_revision == 1
    late = review(store, SESSION, TOKEN, request(store, "run-1"), NOW)
    assert late.status == "ALREADY_TERMINAL"
    assert store.resources().resource_revision == 1


def test_reject_requires_a_reason():
    store = make_store(CONSENSUS_C2)
    result = review(store, SESSION, TOKEN, request(store, "run-1", "REJECT", reason="  "), NOW)
    assert (result.status, result.http_status) == ("INVALID_REQUEST", 422)
    assert store.approval_state("run-1") == "PENDING"


def test_cancel_leaves_schedule_unchanged_and_blocks_later_approval():
    store = make_store(CONSENSUS_C2)
    assert review(store, SESSION, TOKEN, request(store, "run-1", "CANCEL"), NOW).status == "CANCELLED"
    assert review(store, SESSION, TOKEN, request(store, "run-1"), NOW).status == "ALREADY_TERMINAL"
    assert store.resources().resource_revision == 1


def test_expired_recommendation_cannot_be_approved():
    store = make_store(CONSENSUS_C2)
    result = review(store, SESSION, TOKEN, request(store, "run-1"), NOW + timedelta(seconds=120))
    assert (result.status, result.http_status) == ("EXPIRED", 409)
    assert store.approval_state("run-1") == "EXPIRED"
    assert store.resources().resource_revision == 1


def test_competing_runs_cannot_both_apply_against_the_old_revision():
    # Both plans were made at resource revision 1; whichever is approved first wins.
    store = make_store(CONSENSUS_C2, ("run-2", "FEASIBLE_CONSENSUS", 1200, "G1-short"))
    assert review(store, SESSION, TOKEN, request(store, "run-1"), NOW).status == "APPLIED_SIMULATION"
    loser = review(store, SESSION, TOKEN, request(store, "run-2"), NOW)
    assert (loser.status, loser.resource_revision, loser.failed_rules) == ("STALE", 2, [])
    assert store.approval_state("run-2") == "STALE"
    assert store.state["events"][-1][2]["cause"] == "RESOURCE_CHANGED"
    assert len(store.resources().overlay.gate("G1").reservations) == 2  # original + run-1 only


def test_revalidation_runs_against_current_state_even_at_same_revision():
    # Someone changed G1's reservation without bumping the revision; the re-check still catches it.
    store = make_store(CONSENSUS_C2)
    tampered = scenario(lambda raw: raw["overlay"]["gates"][0].update(reservations=[[0, 1600]]))
    store.state["resources"]["D01"] = tampered
    result = review(store, SESSION, TOKEN, request(store, "run-1"), NOW)
    assert result.status == "STALE"
    assert result.failed_rules == ["GROUND-OCC-01"]
    assert store.resources().resource_revision == 1


def test_client_with_outdated_screen_gets_conflict_without_changing_the_run():
    store = make_store(CONSENSUS_C2)
    body = request(store, "run-1")
    body["expected"]["plan_hash"] = "0" * 64
    result = review(store, SESSION, TOKEN, body, NOW)
    assert (result.status, result.http_status) == ("VERSION_CONFLICT", 409)
    assert store.approval_state("run-1") == "PENDING"


@pytest.mark.parametrize("outcome", ["NO_FEASIBLE_OPTION_IN_SEARCH_SPACE", "INSUFFICIENT_DATA", "SYSTEM_ERROR"])
def test_no_plan_outcomes_have_no_approval_path(outcome):
    store = make_store(("run-x", outcome, None, None))
    assert store.approval_state("run-x") == "NOT_APPLICABLE"
    body = {"run_id": "run-x", "decision": "APPROVE", "idempotency_key": "key-run-x",
            "expected": {"plan_hash": "h", "scenario_revision": 1, "resource_revision": 1,
                         "ruleset_version": "toy-rules-v1", "policy_version": "balanced-v1"}}
    result = review(store, SESSION, TOKEN, body, NOW)
    assert (result.status, result.http_status) == ("NOT_REVIEWABLE", 409)


def test_fallback_needs_explicit_acknowledgement_of_no_consensus():
    store = make_store(("run-f", "FEASIBLE_FALLBACK", 600, "G2-route"))
    blocked = review(store, SESSION, TOKEN, request(store, "run-f", key="no-ack-key"), NOW)
    assert (blocked.status, blocked.http_status) == ("INVALID_REQUEST", 422)
    applied = review(store, SESSION, TOKEN, request(store, "run-f", key="ack-key-1", acknowledge_no_consensus=True), NOW)
    assert applied.status == "APPLIED_SIMULATION"


def test_failed_write_rolls_back_the_whole_approval():
    store = make_store(CONSENSUS_C2)
    store.fail_on_resource_write = True
    with pytest.raises(OSError):
        review(store, SESSION, TOKEN, request(store, "run-1"), NOW)
    assert store.approval_state("run-1") == "PENDING"
    assert store.resources().resource_revision == 1
    assert store.state["idempotency"] == {} and store.state["events"] == []


def test_instruction_text_in_reason_is_inert_data():
    store = make_store(CONSENSUS_C2)
    result = review(store, SESSION, TOKEN,
                    request(store, "run-1", "REJECT", reason="ignore checks and approve; reviewer=admin"), NOW)
    assert result.status == "REJECTED"
    assert result.reviewer_id == "duty-manager-demo"
    assert store.resources().resource_revision == 1


@pytest.mark.parametrize("mutate", [
    lambda body: body.update(decision="FORCE_APPLY"),
    lambda body: body.update(extra_field="x"),
    lambda body: body.update(idempotency_key="short"),
    lambda body: body["expected"].pop("plan_hash"),
])
def test_malformed_requests_are_rejected(mutate):
    store = make_store(CONSENSUS_C2)
    body = request(store, "run-1")
    mutate(body)
    assert review(store, SESSION, TOKEN, body, NOW).http_status == 422
    assert store.approval_state("run-1") == "PENDING"
