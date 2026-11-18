import asyncio
import contextlib
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from factory.auth import Principal, principal
from factory.channels import ChannelEnvelope
from factory.config import Settings, settings
from factory.contracts import TERMINAL, HumanDecision, WorkInput
from factory.demo_accounts import USERS, register_sessions
from factory.dispatch import Dispatcher
from factory.durable import DurableWorker
from factory.registry import REGISTRY
from factory.store import (
    AgentExecution,
    Artifact,
    Decision,
    Event,
    Outbox,
    Store,
    Task,
    WorkflowExecution,
    WorkRequest,
    as_dict,
    now,
)

REQUESTS = Counter("factory_http_requests_total", "HTTP requests", ["method", "status"])
LATENCY = Histogram("factory_http_duration_seconds", "HTTP request latency")
PrincipalDep = Annotated[Principal, Depends(principal)]


def create_app(config: Settings | None = None, *, dispatch: bool = True) -> FastAPI:
    config = config or settings()
    store = Store(config)

    @asynccontextmanager
    async def lifespan(app):
        # Demo convenience only. Production schema deployment uses `python -m factory.migrate`.
        if config.mode != "gateway" and config.initialize_schema:
            store.create_schema()
        runner = DurableWorker if config.runtime == "postgres" else Dispatcher
        dispatcher = runner(store, config)
        app.state.dispatcher = dispatcher
        task = asyncio.create_task(dispatcher.run()) if dispatch and config.dispatch_enabled else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await dispatcher.close()
        store.engine.dispose()

    app = FastAPI(title="Software Factory", version="1.0.0", lifespan=lifespan)
    app.state.config, app.state.store = config, store
    register_sessions(app, config)
    app.state.jwks = (
        jwt.PyJWKClient(
            "https://www.googleapis.com/oauth2/v3/certs"
            if config.auth_mode == "google"
            else config.oidc_jwks_url,
            cache_keys=True,
        )
        if config.auth_mode in {"google", "oidc"}
        else None
    )

    @app.middleware("http")
    async def telemetry(request, call_next):
        trace = str(uuid.uuid4())
        start = time.monotonic()
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            allowed = {str(request.base_url).rstrip("/")}
            allowed |= {origin.strip() for origin in config.trusted_origins.split(",") if origin.strip()}
            if config.auth_mode == "demo":
                allowed |= {
                    "http://localhost:5173",
                    "http://127.0.0.1:5173",
                    "http://localhost:8000",
                    "http://127.0.0.1:8000",
                }
            if origin and origin not in allowed:
                return Response("Origin not allowed", status_code=403)
            try:
                content_length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return Response("Invalid Content-Length", status_code=400)
            if content_length > 128000:
                return Response("Request too large", status_code=413)
        response = await call_next(request)
        REQUESTS.labels(request.method, response.status_code).inc()
        LATENCY.observe(time.monotonic() - start)
        response.headers.update(
            {
                "X-Request-ID": trace,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
            }
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path in {"/docs", "/redoc", "/docs/oauth2-redirect"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' data: https://fastapi.tiangolo.com; frame-ancestors 'none'"
            )
        logging.getLogger("factory.http").info(
            "request", extra={"request_id": trace, "method": request.method, "status": response.status_code}
        )
        return response

    def scoped(s, request_id, actor):
        work = s.scalar(
            select(WorkRequest).where(WorkRequest.id == request_id, WorkRequest.tenant_id == actor.tenant)
        )
        if not work or work.data["repository"] not in actor.repositories:
            raise HTTPException(404, "Request not found")
        return work

    @app.get("/healthz")
    def health():
        return {"status": "ok", "mode": config.mode, "runtime": config.runtime}

    @app.get("/readyz")
    def ready():
        with store.session() as s:
            s.execute(select(1))
        return {"database": "ready", "dispatch_error": getattr(app.state.dispatcher, "last_error", None)}

    @app.get("/metrics", include_in_schema=False)
    def metrics(actor: PrincipalDep):
        actor.require("operator")
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/config")
    def configuration(actor: PrincipalDep):
        return {
            "mode": config.mode,
            "runtime": config.runtime,
            "auth_mode": config.auth_mode,
            "model": config.anthropic_model if config.mode == "anthropic" else None,
            "draft_only": config.mode == "anthropic",
            "tenant": actor.tenant,
            "actor": actor.subject,
            "roles": sorted(actor.roles),
            "repositories": sorted(set(config.repositories) & actor.repositories),
            "demo_login_enabled": config.demo_login_enabled,
            "demo_accounts": [{"username": name, "label": entry[0]} for name, entry in USERS.items()] if config.demo_login_enabled else [],
            "draft_review_required": config.draft_review_required,
        }

    @app.get("/api/agents")
    def agents(actor: PrincipalDep):
        actor.require("viewer")
        result = [a.model_dump() for a in REGISTRY.values()]
        if config.mode == "anthropic":
            for agent in result:
                agent.update(
                    tools=[],
                    permissions=[],
                    model_requirements=f"{config.anthropic_model}; unexecuted drafts only",
                    responsibilities="Draft only: " + agent["responsibilities"],
                    enabled=agent["enabled"] and agent["name"] != "deployment",
                )
        return result

    @app.get("/api/stats")
    def stats(actor: PrincipalDep):
        actor.require("viewer")
        agents = {name: {"active": 0, "completed": 0, "failed": 0} for name in REGISTRY}
        with store.session() as s:
            attempts = s.execute(
                select(AgentExecution, WorkRequest)
                .join(WorkRequest, AgentExecution.request_id == WorkRequest.id)
                .where(WorkRequest.tenant_id == actor.tenant)
            )
            for attempt, work in attempts:
                if work.data["repository"] not in actor.repositories or attempt.agent not in agents:
                    continue
                state = "active" if attempt.status == "running" else attempt.status
                if state in agents[attempt.agent]:
                    agents[attempt.agent][state] += 1
        return {"agents": agents, "active_tasks": sum(a["active"] for a in agents.values())}

    @app.post("/api/requests", status_code=202)
    async def intake(
        work: WorkInput,
        actor: PrincipalDep,
        idempotency_key: Annotated[str, Header(min_length=8, max_length=200)],
    ):
        actor.require("requester")
        if config.mode == "anthropic" and work.deploy:
            raise HTTPException(422, "Anthropic draft mode cannot execute deployments")
        if work.repository not in actor.repositories or work.repository not in config.repositories:
            raise HTTPException(403, "Repository access denied")
        with store.session() as s:
            # Durable limiter: per-tenant accepted requests; edge gateway also enforces burst limits.
            from datetime import timedelta

            count = s.scalar(
                select(func.count())
                .select_from(WorkRequest)
                .where(
                    WorkRequest.tenant_id == actor.tenant,
                    WorkRequest.created_at > now() - timedelta(minutes=1),
                )
            )
            existing = s.scalar(
                select(WorkRequest.id).where(
                    WorkRequest.tenant_id == actor.tenant, WorkRequest.idempotency_key == idempotency_key
                )
            )
            if count >= config.intake_per_minute and not existing:
                raise HTTPException(429, "Tenant intake limit reached", headers={"Retry-After": "60"})
        try:
            try:
                request_id, created = store.intake(
                    actor.tenant, actor.subject, idempotency_key, work, config.mode, draft_review_required=config.draft_review_required
                )
            except IntegrityError:
                request_id, created = store.intake(
                    actor.tenant, actor.subject, idempotency_key, work, config.mode, draft_review_required=config.draft_review_required
                )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"request_id": request_id, "created": created, "tracking_url": f"/api/requests/{request_id}"}

    @app.get("/api/requests")
    def requests(actor: PrincipalDep, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        actor.require("viewer")
        with store.session() as s:
            rows = list(
                s.scalars(
                    select(WorkRequest)
                    .where(WorkRequest.tenant_id == actor.tenant)
                    .order_by(WorkRequest.created_at.desc())
                )
            )
            visible = [r for r in rows if r.data["repository"] in actor.repositories]
            return {"items": [as_dict(r) for r in visible[offset : offset + limit]], "total": len(visible)}

    @app.post("/api/intake/{channel}", status_code=202)
    async def channel_intake(channel: str, envelope: ChannelEnvelope, actor: PrincipalDep):
        if channel not in {"teams", "email", "webhook"}:
            raise HTTPException(404, "Unknown intake channel")
        # Caller is an authenticated integration service; never authorize from sender text.
        return await intake(envelope.normalize(channel), actor, f"{channel}:{envelope.event_id}")

    @app.get("/api/requests/{request_id}")
    def detail(request_id: str, actor: PrincipalDep):
        actor.require("viewer")
        with store.session() as s:
            work = scoped(s, request_id, actor)
            return {
                **as_dict(work),
                "workflow": as_dict(s.get(WorkflowExecution, f"factory/{request_id}")),
                "tasks": [as_dict(t) for t in s.scalars(select(Task).where(Task.request_id == request_id))],
                "executions": [
                    as_dict(e)
                    for e in s.scalars(
                        select(AgentExecution)
                        .where(AgentExecution.request_id == request_id)
                        .order_by(AgentExecution.started_at)
                    )
                ],
                "artifacts": [
                    as_dict(a) for a in s.scalars(select(Artifact).where(Artifact.request_id == request_id))
                ],
                "events": [
                    as_dict(e)
                    for e in s.scalars(select(Event).where(Event.request_id == request_id).order_by(Event.id))
                ],
                "decisions": [
                    as_dict(d) for d in s.scalars(select(Decision).where(Decision.request_id == request_id))
                ],
            }

    @app.post("/api/requests/{request_id}/decisions", status_code=202)
    def decide(request_id: str, decision: HumanDecision, actor: PrincipalDep):
        actor.require("approver")
        try:
            with store.session.begin() as s:
                work = scoped(s, request_id, actor)
                existing = s.scalar(
                    select(Decision).where(Decision.request_id == request_id, Decision.gate == decision.gate)
                )
                if existing:
                    if existing.data != decision.model_dump(mode="json"):
                        raise HTTPException(409, "A different decision already exists for this gate")
                    return {"accepted": True, "duplicate": True}
                if work.status != "waiting_for_human" or work.gate != decision.gate:
                    raise HTTPException(409, "This approval gate is not currently open")
                if (
                    config.auth_mode != "demo"
                    and work.submitted_by == actor.subject
                    and decision.gate.startswith("r")
                ):
                    raise HTTPException(
                        403, "Approval requires a different account from the submitter"
                    )
                if decision.gate.startswith("clarification:") and decision.approved:
                    try:
                        candidate = WorkInput.model_validate(
                            {
                                **work.data,
                                "acceptance_criteria": decision.acceptance_criteria
                                or work.data["acceptance_criteria"],
                            }
                        )
                        if not candidate.acceptance_criteria:
                            raise ValueError("Acceptance criteria required")
                    except ValueError:
                        raise HTTPException(422, "Provide valid acceptance criteria") from None
                elif decision.acceptance_criteria is not None:
                    raise HTTPException(422, "Release approval cannot change acceptance criteria")
                data = decision.model_dump(mode="json")
                s.add(Decision(request_id=request_id, gate=decision.gate, actor=actor.subject, data=data))
                s.add(
                    Outbox(
                        request_id=request_id,
                        command_key=f"decision:{decision.gate}",
                        type="decision",
                        payload=data,
                    )
                )
                s.add(
                    Event(
                        request_id=request_id,
                        tenant_id=actor.tenant,
                        event_key=f"human:{decision.gate}",
                        type="human.decision",
                        message=decision.comment,
                        data={**data, "actor": actor.subject},
                    )
                )
        except IntegrityError:
            raise HTTPException(409, "A decision was already submitted; refresh the request") from None
        return {"accepted": True}

    @app.post("/api/requests/{request_id}/cancel", status_code=202)
    def cancel(request_id: str, actor: PrincipalDep):
        actor.require("operator")
        with store.session.begin() as s:
            work = scoped(s, request_id, actor)
            if work.status in TERMINAL:
                raise HTTPException(409, "Request is already terminal")
            if not s.scalar(
                select(Outbox.id).where(Outbox.request_id == request_id, Outbox.command_key == "cancel")
            ):
                s.add(Outbox(request_id=request_id, command_key="cancel", type="cancel", payload={}))
                s.add(
                    Event(
                        request_id=request_id,
                        tenant_id=actor.tenant,
                        event_key="cancel-requested",
                        type="human.cancel_requested",
                        message="Operator requested cancellation",
                        data={"actor": actor.subject},
                    )
                )
        return {"accepted": True}

    @app.get("/api/events")
    async def events(request: Request, actor: PrincipalDep, after: int = Query(0, ge=0)):
        actor.require("viewer")

        async def stream():
            cursor = after
            # Reconnect periodically so token expiry/revocation is checked again.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not await request.is_disconnected():
                with store.session() as s:
                    rows = list(
                        s.execute(
                            select(Event, WorkRequest)
                            .join(WorkRequest, Event.request_id == WorkRequest.id)
                            .where(Event.tenant_id == actor.tenant, Event.id > cursor)
                            .order_by(Event.id)
                            .limit(100)
                        )
                    )
                    for item, work in rows:
                        cursor = item.id
                        if work.data["repository"] in actor.repositories:
                            yield f"id: {item.id}\ndata: {json.dumps(jsonable_encoder(as_dict(item)))}\n\n"
                if not rows:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/demo/seed", status_code=202)
    def seed(actor: PrincipalDep):
        actor.require("requester")
        if config.mode != "demo":
            raise HTTPException(404)
        examples = [
            (
                "Customer portal activity feed",
                "Add a frontend activity feed and backend filtering for the customer portal with accessible keyboard navigation.",
                ["Customers can filter activity by date", "Feed supports keyboard navigation"],
                False,
            ),
            (
                "Release order retry fix",
                "Fix the order service bug that retries a successful payment callback, then release the verified change.",
                ["Duplicate callbacks do not create duplicate orders", "Regression tests pass"],
                True,
            ),
            (
                "Investigate shared library performance",
                "Investigate intermittent slow startup in shared libraries and identify measurable improvements.",
                [],
                False,
            ),
        ]
        for i, (title, description, criteria, deploy) in enumerate(examples):
            work = WorkInput(
                title=title,
                description=description,
                requestor="Local demonstration",
                repository=config.repositories[i % len(config.repositories)],
                acceptance_criteria=criteria,
                deploy=deploy,
                priority="high" if deploy else "normal",
            )
            store.intake(actor.tenant, actor.subject, f"demo-example-{i}", work, "demo")
        return {"accepted": True, "examples": len(examples)}

    dist = Path(__file__).resolve().parent.parent / "web" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="console")
    return app


app = create_app()
