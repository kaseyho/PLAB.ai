"""Frozen, bounded integration evaluation with full-catalog B0/B1 and retained traces.

The default uses explicitly labelled fixture agents. --live uses the configured model
for the six nominal holdout cases; injected faults and stale review remain test doubles.
"""

import argparse
import asyncio
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import JsonValue

from app.agents import AgentProvider, OpenAIProvider, ProviderReply
from app.approvals import current_snapshot, review_plan
from app.contracts import (
    Approval,
    Outcome,
    PlanningHooks,
    ResourceState,
    ReviewRequest,
    Role,
    RunRecord,
    ServiceError,
    StartRunRequest,
    utcnow,
    verify_event_chain,
)
from app.coordinator import Coordinator, Limits
from app.main import Settings
from app.operational_data import file_hash
from app.planning import build_catalog, check_plan, joint_baseline, load_snapshot, select_fallback
from app.store import Store
from examples.backend_fixture import FixtureProvider

ROOT = Path(__file__).resolve().parent.parent
PLANNER = PlanningHooks(build_catalog, check_plan, select_fallback)


class FaultProvider(FixtureProvider):
    def __init__(self, fault: str) -> None:
        self.fault = fault

    async def generate(
        self, role: Role, round_number: int, prompt: dict[str, JsonValue], timeout_s: float
    ) -> ProviderReply:
        if role == Role.PILOT and round_number == 1:
            if self.fault == "timeout":
                await asyncio.sleep(timeout_s + 0.1)
            elif self.fault == "malformed":
                return ProviderReply('{"invented_candidate_id":"NOT_IN_CATALOG"}')
        return await super().generate(role, round_number, prompt, timeout_s)


def stale_review(store: Store, run: RunRecord) -> bool:
    if run.candidate is None or run.expires_at is None:
        return False
    revised = run.snapshot.model_copy(
        update={
            "revision": run.snapshot.revision + 1,
            "resource_revision": run.snapshot.resource_revision + 1,
        }
    )
    store.revise_scenario(current_snapshot(revised, store.get_resource(run.snapshot.resource_id)))
    request = ReviewRequest(
        decision="APPROVE",
        plan_hash=run.candidate.plan_hash,
        scenario_revision=run.snapshot.revision,
        resource_revision=run.snapshot.resource_revision,
        ruleset_version=run.snapshot.ruleset_version,
        policy_version=run.snapshot.policy_version,
        expires_at=run.expires_at,
        reason="Injected stale approval test",
        acknowledge_fallback=True,
    )
    try:
        store.review(run.run_id, request, "stale-attempt", "evaluation-operator", review_plan)
    except ServiceError:
        return (
            store.get_run(run.run_id).approval_state == Approval.STALE
            and not store.get_resource(run.snapshot.resource_id).data
        )
    return False


def case_result(
    store: Store, run: RunRecord, case: dict[str, Any], stale_blocked: bool
) -> dict[str, Any]:
    trace: dict[str, Any] = store.export(run.run_id)
    requests, messages = trace["requests"], trace["messages"]
    assert isinstance(requests, list) and isinstance(messages, list)
    events = store.events(run.run_id)
    expected = case["expected"]
    matches = {
        "FEASIBLE": run.negotiation_outcome in (Outcome.CONSENSUS, Outcome.FALLBACK),
        "NO_PLAN": run.negotiation_outcome == Outcome.NO_PLAN,
        "INSUFFICIENT": run.negotiation_outcome == Outcome.INSUFFICIENT,
        "FALLBACK": run.negotiation_outcome == Outcome.FALLBACK,
        "STALE_BLOCKED": stale_blocked,
    }[expected]
    feasible = run.negotiation_outcome in (Outcome.CONSENSUS, Outcome.FALLBACK)
    checks_complete = not feasible or (
        {c.rule_id for c in run.checks} == set(run.snapshot.required_rule_ids)
        and all(c.status == "PASS" for c in run.checks)
    )
    pairs = [(request["round"], request["role"]) for request in requests]
    bounded = (
        run.round_count <= 3 and run.agent_invocation_count <= 9 and len(pairs) == len(set(pairs))
    )
    fallback_index = next(
        (
            e.sequence
            for e in events
            if e.event_type == "STATE_CHANGED" and e.payload.get("state") == "FALLBACK"
        ),
        None,
    )
    fallback_no_calls = fallback_index is None or not any(
        e.event_type == "AGENT_REQUEST_STARTED" and e.sequence > fallback_index for e in events
    )
    unanimous = run.negotiation_outcome != Outcome.CONSENSUS or (
        set(run.votes) == set(Role)
        and all(v.decision == "ACCEPT" and v.plan_hash == run.plan_hash for v in run.votes.values())
    )
    no_application = not store.get_resource(run.snapshot.resource_id).data
    chain_valid = verify_event_chain(events)
    fault_matches = (case["variant"] != "timeout" or run.fallback_reason == "AGENT_TIMEOUT") and (
        case["variant"] != "malformed" or run.fallback_reason == "INVALID_AGENT_SCHEMA"
    )
    passed = all(
        (
            matches,
            checks_complete,
            bounded,
            fallback_no_calls,
            unanimous,
            no_application,
            chain_valid,
            fault_matches,
        )
    )
    baselines: dict[str, Any] = {"B0": None, "B1": None}
    if run.catalog:
        b0_id = select_fallback(run.snapshot, run.catalog).candidate_id
        b0 = next((e for e in run.catalog.candidates if e.plan.candidate_id == b0_id), None)
        b1 = joint_baseline(run.catalog)
        for name, entry in (("B0", b0), ("B1", b1)):
            if entry is not None:
                baselines[name] = {
                    "candidate_id": entry.plan.candidate_id,
                    "costs": entry.plan.costs.model_dump(mode="json"),
                    "impacts": entry.plan.impacts.model_dump(mode="json"),
                }
    comparison: dict[str, Any] = {}
    if run.candidate and baselines["B0"]:
        comparison["vs_B0"] = {
            key: str(value - type(value)(baselines["B0"]["impacts"][key]))
            for key, value in run.candidate.impacts.model_dump().items()
        }
    if run.candidate and baselines["B1"]:
        from decimal import Decimal

        comparison["joint_max_cost_regret_vs_B1"] = str(
            max(run.candidate.costs.model_dump().values())
            - max(Decimal(value) for value in baselines["B1"]["costs"].values())
        )
    return {
        "case_id": case["case_id"],
        "variant": case["variant"],
        "expected": expected,
        "passed": passed,
        "run_id": run.run_id,
        "outcome": run.negotiation_outcome,
        "fallback_reason": run.fallback_reason,
        "provider": run.provider.model_dump(mode="json"),
        "turns": run.round_count,
        "agent_invocations": run.agent_invocation_count,
        "model_calls": run.model_call_count,
        "elapsed_ms": run.elapsed_ms,
        "snapshot_hash": run.snapshot.snapshot_hash,
        "dataset_sha256": run.snapshot.source.dataset_sha256,
        "checks_complete": checks_complete,
        "unanimous_current_votes": unanimous,
        "bounded": bounded,
        "fallback_has_no_calls": fallback_no_calls,
        "no_automatic_application": no_application,
        "audit_chain_valid": chain_valid,
        "stale_approval_blocked": stale_blocked,
        "candidate_id": run.candidate.candidate_id if run.candidate else None,
        "baselines": baselines,
        "comparison": comparison,
        "input_tokens_reported": sum(r.get("input_tokens") or 0 for r in requests),
        "output_tokens_reported": sum(r.get("output_tokens") or 0 for r in requests),
        "requests_without_token_usage": sum(r.get("input_tokens") is None for r in requests),
        "trace": f"{case['case_id']}-{run.run_id}.json",
    }


async def evaluate(
    output: Path, provider: AgentProvider, *, split: str = "holdout", data_dir: Path = ROOT / "data"
) -> dict[str, Any]:
    manifest_path = ROOT / "evals/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    cases = [case for case in manifest["cases"] if case["split"] == split]
    # Validate the entire frozen input set before issuing any provider request.
    snapshots = {}
    for case in cases:
        path = ROOT / case["path"]
        if file_hash(path) != case["sha256"]:
            raise ValueError(f"Frozen scenario changed: {case['case_id']}")
        snapshots[case["case_id"]] = load_snapshot(path, data_dir)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    results = []
    for case in cases:
        snapshot = snapshots[case["case_id"]]
        # Fault injection is explicitly a fixture, even in a live suite.
        selected: AgentProvider = (
            provider if case["expected"] == "FEASIBLE" else FaultProvider(case["variant"])
        )
        limits = Limits(request_timeout_s=0.05) if case["variant"] == "timeout" else Limits()
        store = Store(output / f"{case['case_id']}-{uuid4()}.sqlite3")
        store.register_scenario(
            snapshot, ResourceState(resource_id=snapshot.resource_id, revision=1)
        )
        run, _ = store.create_run(
            StartRunRequest(scenario_id=snapshot.scenario_id, scenario_revision=1),
            str(uuid4()),
            selected.info,
            "INTEGRATED",
            limits.deadline_s,
        )
        run = await Coordinator(store, PLANNER, selected, limits).run(run.run_id)
        blocked = stale_review(store, run) if case["variant"] == "stale_approval" else False
        result = case_result(store, run, case, blocked)
        (output / result["trace"]).write_text(json.dumps(store.export(run.run_id), indent=2) + "\n")
        results.append(result)
        print(
            f"{case['case_id']}: {run.negotiation_outcome}; {run.round_count} turns; "
            f"{run.model_call_count} model calls; pass={result['passed']}",
            flush=True,
        )
    nominal = [r for r in results if r["expected"] == "FEASIBLE"]
    live = [r for r in nominal if r["provider"]["mode"] == "LIVE"]
    latency = sorted(r["elapsed_ms"] for r in nominal if r["elapsed_ms"] is not None)
    passed = sum(r["passed"] for r in results)
    live_consensus = sum(r["outcome"] == Outcome.CONSENSUS for r in live)
    live_calls = sum(r["model_calls"] for r in results)
    report = {
        "schema_version": 1,
        "status": "COMPLETED" if passed == len(results) else "FAILED",
        "created_at": utcnow().isoformat(),
        "split": split,
        "manifest_sha256": file_hash(manifest_path),
        "dataset": manifest["dataset"],
        "workbook": manifest["workbook"],
        "provider_mode": provider.info.mode,
        "provider": provider.info.model_dump(mode="json"),
        "summary": f"{passed}/{len(results)} frozen {split} contract cases passed. "
        f"Live nominal consensus: {live_consensus}/{len(live)}. "
        f"{live_calls} real model requests. Fault injections use labelled fixtures.",
        "counts": {
            "passed": passed,
            "total": len(results),
            "nominal": len(nominal),
            "live_nominal": len(live),
            "live_consensus": live_consensus,
            "model_calls": live_calls,
        },
        "outcomes": dict(Counter(r["outcome"] for r in results)),
        "nominal_latency_ms": {
            "n": len(latency),
            "median": statistics.median(latency) if latency else None,
            "p95": latency[math.ceil(len(latency) * 0.95) - 1] if latency else None,
        },
        "limitations": [
            manifest["selection"],
            "Expected outcome classes are frozen; boundary tests independently check numeric "
            "rules. This is not an aviation safety certification.",
            "Only nominal LIVE rows measure model coordination. Fault fixtures are not model "
            "performance. No optional repeated-case stability experiment was run.",
            "Timeout case injects a 50ms request deadline. All other cases use the "
            "12s/request, 45s/run and 5s fallback reserve profile.",
            "B0 and B1 use the identical full catalog. Comparisons are simulated effects, "
            "not measured historical savings.",
        ],
        "cases": results,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


async def main_async(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    if args.live and not (settings.api_key and settings.model):
        raise ValueError("Configure a provider key and model locally before --live")
    provider: AgentProvider = (
        OpenAIProvider(
            settings.api_key, settings.model, base_url=settings.base_url, api_mode=settings.api_mode
        )
        if args.live
        else FixtureProvider()
    )
    try:
        report = await evaluate(args.output, provider, split=args.split, data_dir=settings.data_dir)
    finally:
        await provider.close()
    settings.evaluation_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic publication; incomplete runs never replace the last complete report.
    temporary = settings.evaluation_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(settings.evaluation_path)
    print(report["summary"])
    if report["status"] != "COMPLETED":
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="At most 54 live requests for the six nominal holdout cases",
    )
    parser.add_argument("--split", choices=("development", "holdout"), default="holdout")
    parser.add_argument("--output", type=Path, default=Path("artifacts/evals") / str(uuid4()))
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
