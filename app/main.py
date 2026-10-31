"""Local FastAPI composition root. A and C connect through two explicit hooks."""

from __future__ import annotations

import asyncio
import hmac
import importlib
import json
import logging
import os
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import cast

import psycopg
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import JsonValue, ValidationError
from starlette.middleware.base import RequestResponseEndpoint

from app.agents import AgentProvider, OpenAIProvider, UnavailableProvider
from app.contracts import (
    ApiMode,
    PlanningHooks,
    ResourceState,
    ReviewHook,
    ReviewRequest,
    RunRecord,
    ScenarioSnapshot,
    ServiceError,
    StartRunRequest,
    State,
    utcnow,
)
from app.coordinator import Coordinator, Limits
from app.judge_access import COOKIE_NAME, SESSION_SECONDS, JudgeAccess
from app.store import Store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path("artifacts/organizer.sqlite3")
    scenario_dir: Path = Path("scenarios/organizer")
    data_dir: Path = Path("data")
    static_dir: Path = Path("app/static")
    evaluation_path: Path = Path("artifacts/evals/latest.json")
    public_origin: str = "http://127.0.0.1:8000"
    reviewer_id: str = "local-duty-manager"
    session_token: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    model: str = ""
    base_url: str = "https://api.openai.com/v1"
    api_mode: ApiMode = "responses"
    public_demo: bool = False
    daily_run_limit: int = 100
    database_url: str = field(default="", repr=False)
    request_bound_runs: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(override=False)
        vercel_host = os.getenv(
            "VERCEL_PROJECT_PRODUCTION_URL"
            if os.getenv("VERCEL_ENV") == "production"
            else "VERCEL_URL",
            "",
        )
        default_origin = os.getenv("RENDER_EXTERNAL_URL") or (
            f"https://{vercel_host}" if vercel_host else "http://127.0.0.1:8000"
        )
        provider = "SOCLAAS" if os.getenv("SOCLAAS_API_KEY") else "OPENAI"
        default_base = (
            "https://soclaas-api.comp.nus.edu.sg/v1"
            if provider == "SOCLAAS"
            else "https://api.openai.com/v1"
        )
        api_mode = os.getenv(
            f"{provider}_API_MODE", "chat_completions" if provider == "SOCLAAS" else "responses"
        )
        if api_mode not in ("responses", "chat_completions"):
            raise ValueError("Provider API_MODE must be responses or chat_completions")
        return cls(
            db_path=Path(os.getenv("PLAB_DB_PATH", "artifacts/organizer.sqlite3")),
            data_dir=Path(os.getenv("PLAB_DATA_DIR", "data")),
            evaluation_path=Path(os.getenv("PLAB_EVALUATION_PATH", "artifacts/evals/latest.json")),
            public_origin=os.getenv("PLAB_PUBLIC_ORIGIN", default_origin).rstrip("/"),
            reviewer_id=os.getenv("PLAB_REVIEWER_ID", "local-duty-manager"),
            session_token=os.getenv("PLAB_SESSION_TOKEN", ""),
            api_key=os.getenv(f"{provider}_API_KEY", ""),
            model=os.getenv(f"{provider}_MODEL", ""),
            base_url=os.getenv(f"{provider}_BASE_URL", default_base),
            api_mode=cast(ApiMode, api_mode),
            public_demo=os.getenv("PLAB_PUBLIC_DEMO", "false").lower() == "true",
            daily_run_limit=int(os.getenv("PLAB_DAILY_RUN_LIMIT", "100")),
            database_url=os.getenv("DATABASE_URL", ""),
            request_bound_runs=os.getenv("VERCEL") == "1",
        )


def discover_planner() -> PlanningHooks | None:
    try:
        planning = importlib.import_module("app.planning")
    except ModuleNotFoundError as error:
        if error.name in {"app.planning", "app.constraints"}:
            return None
        raise
    return PlanningHooks(planning.build_catalog, planning.check_plan, planning.select_fallback)


def discover_reviewer() -> ReviewHook | None:
    try:
        approvals = importlib.import_module("app.approvals")
    except ModuleNotFoundError as error:
        if error.name == "app.approvals":
            return None
        raise
    return cast(ReviewHook, approvals.review_plan)


def create_app(
    *,
    settings: Settings | None = None,
    store: Store | None = None,
    planner: PlanningHooks | None = None,
    reviewer: ReviewHook | None = None,
    provider: AgentProvider | None = None,
    limits: Limits | None = None,
) -> FastAPI:
    config = settings or Settings.from_env()
    if config.request_bound_runs and not config.database_url and store is None:
        raise ValueError("Vercel requires DATABASE_URL for persistent run state")
    database = store or Store(config.db_path, database_url=config.database_url)
    judges = (
        JudgeAccess(database, config.session_token, config.daily_run_limit)
        if config.public_demo
        else None
    )
    planning = planner or discover_planner()
    review_hook = reviewer or discover_reviewer()
    agent_provider: AgentProvider = provider or (
        OpenAIProvider(
            config.api_key, config.model, base_url=config.base_url, api_mode=config.api_mode
        )
        if config.api_key and config.model
        else UnavailableProvider()
    )
    budgets = limits or Limits()
    coordinator = Coordinator(database, planning, agent_provider, budgets) if planning else None
    tasks: dict[str, asyncio.Task[RunRecord]] = {}
    scenario_errors: list[JsonValue] = []
    templates: list[ScenarioSnapshot] = []

    def task_finished(run_id: str, task: asyncio.Task[RunRecord]) -> None:
        tasks.pop(run_id, None)
        if not task.cancelled() and task.exception():
            logger.error("Run persistence failed: %s", type(task.exception()).__name__)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database.recover_interrupted(expired_only=config.request_bound_runs)
        from app.planning import load_snapshot

        for path in sorted(config.scenario_dir.glob("*.json")):
            try:
                snapshot = load_snapshot(path, config.data_dir)
            except ServiceError as error:
                scenario_errors.append({"file": path.name, "code": error.code})
                continue
            templates.append(snapshot)
            database.register_scenario(
                snapshot,
                ResourceState(
                    resource_id=snapshot.resource_id, revision=snapshot.resource_revision
                ),
            )
        yield
        pending = list(tasks.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await agent_provider.close()

    application = FastAPI(title="PLAB coordination API", version="0.1.0", lifespan=lifespan)
    application.state.store = database
    application.state.coordinator = coordinator

    @application.middleware("http")
    async def private_api_responses(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(ServiceError)
    async def service_error(request: Request, error: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content={"error": {"code": error.code}})

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        # Field locations/types are sufficient; never echo a secret-bearing input body.
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_INPUT",
                    "fields": [
                        {"location": list(e["loc"]), "type": e["type"]} for e in error.errors()
                    ],
                }
            },
        )

    @application.exception_handler(sqlite3.Error)
    @application.exception_handler(psycopg.Error)
    async def storage_error(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(status_code=503, content={"error": {"code": "STORAGE_UNAVAILABLE"}})

    async def authorize(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin is not None and origin != config.public_origin:
            raise ServiceError("ORIGIN_NOT_ALLOWED", 403)
        if judges:
            if origin != config.public_origin:
                raise ServiceError("ORIGIN_NOT_ALLOWED", 403)
            judges.owner(request.cookies.get(COOKIE_NAME))
            return
        supplied = request.headers.get("authorization", "")
        if len(config.session_token) < 16 or not hmac.compare_digest(
            supplied,
            "Bearer " + config.session_token,
        ):
            raise ServiceError("REVIEWER_SESSION_REQUIRED", 403)

    def idempotency_key(request: Request) -> str:
        key = request.headers.get("idempotency-key", "")
        if not 1 <= len(key) <= 128:
            raise ServiceError("IDEMPOTENCY_KEY_REQUIRED", 422)
        return f"{guest_owner(request)}:{key}" if judges else key

    def guest_owner(request: Request) -> str:
        return judges.owner(request.cookies.get(COOKIE_NAME)) if judges else ""

    def require_scope(request: Request, identifier: str) -> None:
        if judges:
            judges.require_scope(guest_owner(request), identifier)

    def owned_run(request: Request, run_id: str) -> RunRecord:
        record = database.get_run(run_id)
        require_scope(request, record.snapshot.scenario_id)
        return record

    def run_response(run: RunRecord) -> dict[str, JsonValue]:
        payload = cast(dict[str, JsonValue], run.model_dump(mode="json"))
        payload["plan_hash"] = run.plan_hash
        payload["snapshot_hash"] = run.snapshot.snapshot_hash
        payload["scenario_revision"] = run.snapshot.revision
        payload["resource_revision"] = run.snapshot.resource_revision
        payload["ruleset_version"] = run.snapshot.ruleset_version
        payload["policy_version"] = run.snapshot.policy_version
        payload["candidate_set_hash"] = run.catalog.catalog_hash if run.catalog else None
        return payload

    mutations = [Depends(authorize)]

    def require_sources(snapshot: ScenarioSnapshot) -> None:
        from app.operational_data import verify_source_files

        try:
            verify_source_files(snapshot, config.data_dir)
        except ServiceError:
            database.invalidate_resource(snapshot.resource_id)
            raise

    @application.get("/health")
    async def health() -> dict[str, JsonValue]:
        database.list_scenarios()  # Verifies storage is readable, without exposing its path.
        return {
            "status": "degraded" if scenario_errors else "ok",
            "scenario_load_errors": scenario_errors,
            "planning_connected": planning is not None,
            "review_connected": review_hook is not None,
            "provider_mode": agent_provider.info.mode,
            "model": agent_provider.info.model,
            "session_configured": len(config.session_token) >= 16,
            "public_demo": config.public_demo,
            "execution_mode": "REQUEST_BOUND" if config.request_bound_runs else "BACKGROUND",
        }

    @application.get("/api/session")
    async def session(request: Request) -> JSONResponse:
        response = JSONResponse(
            {"public_demo": config.public_demo}, headers={"Cache-Control": "no-store"}
        )
        if judges:
            if request.headers.get("sec-fetch-site") == "cross-site":
                raise ServiceError("ORIGIN_NOT_ALLOWED", 403)
            _, cookie = judges.open(
                request.cookies.get(COOKIE_NAME),
                request.client.host if request.client else "unknown",
                templates,
            )
            response.set_cookie(
                COOKIE_NAME,
                cookie,
                httponly=True,
                secure=config.public_origin.startswith("https://"),
                samesite="strict",
                max_age=SESSION_SECONDS,
            )
        return response

    @application.get("/api/scenarios", response_model=list[ScenarioSnapshot])
    async def scenarios(request: Request) -> list[ScenarioSnapshot]:
        return database.list_scenarios(guest_owner(request) + ":" if judges else "")

    @application.get("/api/scenarios/{scenario_id}", response_model=ScenarioSnapshot)
    async def scenario(
        scenario_id: str, request: Request, revision: int | None = None
    ) -> ScenarioSnapshot:
        require_scope(request, scenario_id)
        return database.get_scenario(scenario_id, revision)

    @application.get("/api/resources/{resource_id}", response_model=ResourceState)
    async def resource(resource_id: str, request: Request) -> ResourceState:
        require_scope(request, resource_id)
        return database.get_resource(resource_id)

    @application.post(
        "/api/scenarios/{scenario_id}/revisions",
        dependencies=mutations,
        response_model=ScenarioSnapshot,
        status_code=201,
    )
    async def revise(
        scenario_id: str, snapshot: ScenarioSnapshot, request: Request
    ) -> ScenarioSnapshot:
        require_scope(request, scenario_id)
        if scenario_id != snapshot.scenario_id:
            raise ServiceError("SCENARIO_ID_MISMATCH", 422)
        previous = database.get_scenario(scenario_id)
        if (
            previous.ruleset_version,
            previous.policy_version,
            previous.concession_delta,
            previous.required_rule_ids,
            previous.required_fact_ids,
        ) != (
            snapshot.ruleset_version,
            snapshot.policy_version,
            snapshot.concession_delta,
            snapshot.required_rule_ids,
            snapshot.required_fact_ids,
        ) or [f for f in previous.facts if f.provenance == "policy"] != [
            f for f in snapshot.facts if f.provenance == "policy"
        ]:
            raise ServiceError("POLICY_EDIT_REQUIRES_TRUSTED_SETUP", 422)
        from app.approvals import current_snapshot

        try:
            snapshot = current_snapshot(snapshot, database.get_resource(snapshot.resource_id))
        except (KeyError, ValidationError) as error:
            raise ServiceError("INVALID_INPUT", 422) from error
        result = database.revise_scenario(snapshot)
        for run_id, task in list(tasks.items()):
            if database.get_run(run_id).processing_state == "TERMINAL":
                task.cancel()
        return result

    @application.post("/api/runs", dependencies=mutations, status_code=202)
    async def start(body: StartRunRequest, request: Request) -> dict[str, JsonValue]:
        accepted_at = time.monotonic()
        if coordinator is None or planning is None:
            raise ServiceError("PLANNING_NOT_CONNECTED", 503)
        require_scope(request, body.scenario_id)
        if body.parent_run_id:
            owned_run(request, body.parent_run_id)
        key = idempotency_key(request)
        if existing := database.existing_run(body, key):
            return run_response(existing)
        require_sources(database.get_scenario(body.scenario_id, body.scenario_revision))
        guard = partial(judges.check_run_budget, guest_owner(request)) if judges else None
        run, created = database.create_run(
            body, key, agent_provider.info, planning.mode, budgets.deadline_s, guard=guard
        )
        if created and not config.request_bound_runs:
            task = asyncio.create_task(coordinator.run(run.run_id, accepted_at))
            tasks[run.run_id] = task
            task.add_done_callback(lambda done: task_finished(run.run_id, done))
        return run_response(run)

    @application.post("/api/runs/{run_id}/execute", dependencies=mutations)
    async def execute(run_id: str, request: Request) -> Response:
        """Keep serverless execution inside a streamed request, never a detached task."""
        record = owned_run(request, run_id)
        if not config.request_bound_runs or coordinator is None:
            raise ServiceError("EXECUTION_MODE_UNAVAILABLE", 409)
        if not database.claim_execution(run_id):
            return Response(status_code=204)
        accepted_at = time.monotonic() - max(0, (utcnow() - record.created_at).total_seconds())

        async def stream() -> AsyncIterator[bytes]:
            task = asyncio.create_task(coordinator.run(run_id, accepted_at))
            tasks[run_id] = task
            try:
                yield b": accepted\n\n"
                while not task.done():
                    await asyncio.wait({task}, timeout=1)
                    if (
                        not task.done()
                        and database.get_run(run_id).processing_state == State.TERMINAL
                    ):
                        task.cancel()
                    yield b": running\n\n"
                await asyncio.gather(task, return_exceptions=True)
                yield b"event: finished\ndata: {}\n\n"
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                tasks.pop(run_id, None)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @application.get("/api/runs/{run_id}")
    async def run(run_id: str, request: Request) -> dict[str, JsonValue]:
        return run_response(owned_run(request, run_id))

    @application.get("/api/runs/{run_id}/events")
    async def events(
        run_id: str, request: Request, after_seq: int = 0
    ) -> list[dict[str, JsonValue]]:
        owned_run(request, run_id)
        if after_seq < 0:
            raise ServiceError("INVALID_EVENT_SEQUENCE", 422)
        return [
            cast(dict[str, JsonValue], e.model_dump(mode="json"))
            for e in database.events(run_id, after_seq)
        ]

    @application.post("/api/runs/{run_id}/cancel", dependencies=mutations)
    async def cancel(run_id: str, request: Request) -> dict[str, JsonValue]:
        owned_run(request, run_id)
        result = database.cancel(run_id)
        if task := tasks.get(run_id):
            task.cancel()
        return run_response(result)

    @application.post("/api/runs/{run_id}/decision", dependencies=mutations)
    async def decision(run_id: str, body: ReviewRequest, request: Request) -> dict[str, JsonValue]:
        owned_run(request, run_id)
        if review_hook is None:
            raise ServiceError("REVIEW_NOT_CONNECTED", 503)
        if body.decision == "APPROVE":
            require_sources(database.get_run(run_id).snapshot)
        result = database.review(
            run_id,
            body,
            idempotency_key(request),
            guest_owner(request) if judges else config.reviewer_id,
            review_hook,
        )
        return run_response(result)

    @application.get("/api/runs/{run_id}/export")
    async def export(run_id: str, request: Request) -> JSONResponse:
        filename = f"run-{owned_run(request, run_id).run_id}.json"
        return JSONResponse(
            database.export(run_id),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @application.get("/api/evaluations/latest")
    async def evaluation() -> dict[str, JsonValue]:
        if not config.evaluation_path.exists():
            return {"status": "NOT_YET_EVALUATED"}
        try:
            report = json.loads(config.evaluation_path.read_text())
            if not isinstance(report, dict):
                raise ValueError("Report must be an object")
            return cast(dict[str, JsonValue], report)
        except (ValueError, ValidationError) as error:
            raise ServiceError("EVALUATION_REPORT_INVALID", 503) from error

    @application.get("/", response_model=None)
    async def index() -> FileResponse | dict[str, str]:
        path = config.static_dir / "index.html"
        if path.is_file():
            return FileResponse(path)
        return {
            "status": "BACKEND_READY",
            "dashboard": "Developer A integration pending",
            "api_docs": "/docs",
        }

    if config.static_dir.is_dir():
        application.mount("/static", StaticFiles(directory=config.static_dir), name="static")
    return application
