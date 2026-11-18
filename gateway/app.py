"""HTTP surface of the execution gateway.

POST /execute starts an agent call or rejoins one already known by its idempotency key. A call that
is still working answers 202; the factory repeats the same request until it gets the result.
"""

import asyncio
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from factory.contracts import AgentOutput, Plan, Triage, WorkInput
from factory.registry import REGISTRY
from gateway import agents
from gateway.config import Settings, settings
from gateway.model import Meter, Model, Rejected
from gateway.store import Store

log = logging.getLogger("gateway")
SHAPES = {"triage": Triage, "planner": Plan}


def reply(code: int, **content) -> JSONResponse:
    headers = {"Retry-After": "2"} if code == 202 else None
    return JSONResponse(content, status_code=code, headers=headers)


def create_app(config: Settings | None = None) -> FastAPI:
    config = config or settings()
    store = Store(config.database_url)
    model = Model(config)
    tasks: dict[str, asyncio.Task] = {}

    @asynccontextmanager
    async def lifespan(app):
        interrupted = store.interrupt_running()
        if interrupted:
            log.warning("interrupted_operations", extra={"count": interrupted})
        yield
        for task in list(tasks.values()):
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        store.engine.dispose()

    app = FastAPI(title="Software Factory execution gateway", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store, app.state.tasks = store, tasks

    async def work(op_id: str, body: dict):
        meter = Meter(config.model, float(body.get("max_cost_usd", 2)))
        call = agents.Call(config, store, model, meter, body, config.repositories[body["work"]["repository"]], op_id)
        try:
            output = await asyncio.wait_for(agents.execute(call), config.operation_timeout_seconds)
            output = SHAPES.get(body["agent"], AgentOutput).model_validate(output).model_dump(mode="json")
            store.finish(op_id, "succeeded", result=output, cost=meter.cost)
        except asyncio.CancelledError:
            store.finish(op_id, "failed", error="Gateway stopped before this finished", cost=meter.cost)
            raise
        except Rejected as exc:
            store.finish(op_id, "rejected", error=str(exc)[:500], cost=meter.cost)
        except TimeoutError:
            store.finish(op_id, "failed", error="Operation exceeded the gateway time limit", cost=meter.cost)
        except Exception as exc:
            log.exception("operation_failed", extra={"operation": op_id})
            store.finish(op_id, "failed", error=f"{type(exc).__name__}: {str(exc)[:500]}", cost=meter.cost)
        finally:
            tasks.pop(op_id, None)

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "running": len(tasks), "sandbox": config.sandbox, "model": config.model}

    @app.post("/execute")
    async def execute(request: Request):
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Bearer {config.token.get_secret_value()}".encode()):
            return reply(401, error="Invalid gateway credentials")
        raw = await request.body()
        if len(raw) > 2_000_000:
            return reply(422, error="Request body is larger than 2 MB")
        try:
            body = json.loads(raw)
            work_input = WorkInput.model_validate(body["work"])
            identity = [str(body[field]) for field in ("tenant_id", "request_id", "key", "agent")]
        except (ValueError, KeyError, TypeError, ValidationError):
            return reply(422, error="Request does not match the gateway contract")
        tenant, request_id, key, agent = identity
        if body.get("schema_version") != 1:
            return reply(422, error="Unsupported schema_version")
        if request.headers.get("idempotency-key") != f"{request_id}:{key}":
            return reply(422, error="Idempotency-Key must be '<request_id>:<key>'")
        # Tools and permissions come from this server's registry; the definition in the body is ignored.
        if agent not in REGISTRY or not REGISTRY[agent].enabled:
            return reply(422, error=f"Agent is not registered: {agent}")
        if work_input.repository not in config.repositories:
            return reply(403, error="Repository is not configured in this gateway")

        op_id = f"{tenant}:{request_id}:{key}"
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        op, task = store.get(op_id), tasks.get(op_id)
        if op and op.body_hash != digest:
            return reply(422, error="This idempotency key was already used with a different request")
        if op and op.status == "succeeded":
            return reply(200, **op.result)
        if op and op.status == "rejected":
            return reply(422, error=op.error)
        if task is None:
            # First sight of this call, or an earlier attempt failed or was lost with a previous process.
            if store.spent(request_id) >= work_input.budget_usd:
                return reply(422, error="The request's budget is already spent")
            store.start(op_id, body, digest)  # the receipt exists before any work does
            task = tasks[op_id] = asyncio.create_task(work(op_id, body))
        await asyncio.wait({task}, timeout=config.wait_seconds)
        if not task.done():
            return reply(202, status="running")
        op = store.get(op_id)
        if op.status == "succeeded":
            return reply(200, **op.result)
        return reply(422 if op.status == "rejected" else 502, error=op.error)

    return app
