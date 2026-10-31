"""Explicit fixture-planner smoke test or local API, with optional live role calls."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import uvicorn

from app.agents import AgentProvider, OpenAIProvider
from app.contracts import ResourceState, StartRunRequest
from app.coordinator import Coordinator
from app.main import Settings, create_app
from app.store import Store
from examples.backend_fixture import (
    FixtureProvider,
    fixture_planner,
    fixture_review,
    sample_snapshot,
)


async def smoke(store: Store, provider: AgentProvider, output: Path) -> None:
    snapshot = sample_snapshot()
    run, _ = store.create_run(
        StartRunRequest(scenario_id=snapshot.scenario_id, scenario_revision=snapshot.revision),
        str(uuid4()),
        provider.info,
        "FIXTURE",
        45,
    )
    try:
        result = await Coordinator(store, fixture_planner(), provider).run(run.run_id)
        trace = store.export(run.run_id)
        path = output / f"{run.run_id}.json"
        path.write_text(json.dumps(trace, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "run_id": run.run_id,
                    "planning_mode": result.planning_mode,
                    "provider_mode": result.provider.mode,
                    "outcome": result.negotiation_outcome,
                    "fallback_reason": result.fallback_reason,
                    "agent_invocations": result.agent_invocation_count,
                    "model_calls": result.model_call_count,
                    "elapsed_ms": result.elapsed_ms,
                    "trace": str(path.resolve()),
                },
                indent=2,
            )
        )
    finally:
        await provider.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use configured real model calls")
    parser.add_argument("--serve", action="store_true", help="Serve fixture API on localhost:8000")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.live and not (settings.api_key and settings.model):
        parser.error("Configure SOCLAAS_API_KEY/SOCLAAS_MODEL or OPENAI_API_KEY/OPENAI_MODEL")
    provider: AgentProvider = (
        OpenAIProvider(
            settings.api_key, settings.model, base_url=settings.base_url, api_mode=settings.api_mode
        )
        if args.live
        else FixtureProvider()
    )
    # An isolated database per invocation prevents demo state mixing with integrated runs.
    output = Path("artifacts/backend-smoke")
    settings = replace(
        settings, db_path=output / f"{uuid4()}.sqlite3", scenario_dir=Path("examples/unused")
    )
    store = Store(settings.db_path)
    snapshot = sample_snapshot()
    store.register_scenario(snapshot, ResourceState(resource_id=snapshot.resource_id, revision=1))
    if args.serve:
        application = create_app(
            settings=settings,
            store=store,
            planner=fixture_planner(),
            reviewer=fixture_review,
            provider=provider,
        )
        uvicorn.run(application, host="127.0.0.1", port=8000)
    else:
        asyncio.run(smoke(store, provider, output))


if __name__ == "__main__":
    main()
