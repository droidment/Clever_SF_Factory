"""In-process runtime for local demos and drafts. Requests live in this process's memory;
use the Postgres runtime (factory.durable) when work must survive a restart or span workers."""

import asyncio
import logging

from sqlalchemy import select, text

from factory.agents import AgentService, PermanentAgentError
from factory.contracts import TERMINAL
from factory.engine import Supervisor
from factory.store import Decision, Outbox, WorkflowExecution, WorkRequest

log = logging.getLogger("factory.dispatch")


class DemoRuntime:
    """Local demo/draft replay; explicitly not a substitute for the durable Postgres runtime."""

    def __init__(self, store, config, payload):
        self.store, self.payload, self.cancelled = store, payload, False
        self.agents = AgentService(store, config)
        self.max_attempts = 1 if config.mode == "anthropic" else 3

    async def emit(self, key, type_, message, data):
        self.store.project(self.payload["request_id"], self.payload["tenant_id"], key, type_, message, data)

    async def invoke(self, payload):
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await self.agents.invoke(payload, attempt)
            except (ValueError, PermanentAgentError):
                raise
            except Exception:
                if attempt == self.max_attempts:
                    raise
                await asyncio.sleep(0.1 * attempt)

    async def decision(self, gate):
        while not self.cancelled:
            with self.store.session() as s:
                found = s.scalar(
                    select(Decision).where(
                        Decision.request_id == self.payload["request_id"], Decision.gate == gate
                    )
                )
                if found:
                    return found.data
            await asyncio.sleep(0.25)
        return {"approved": False, "comment": "Cancelled"}


class Dispatcher:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.tasks, self.runtimes = {}, {}
        self.last_error = None
        self.lease = None

    async def tick(self):
        self.last_error = None
        with self.store.session() as s:
            # Never turn previously accepted demo requests into paid Anthropic calls,
            # or silently execute an interrupted AI request with simulated agents.
            same_provider = (
                WorkflowExecution.engine == "anthropic"
                if self.config.mode == "anthropic"
                else WorkflowExecution.engine != "anthropic"
            )
            commands = list(
                s.scalars(
                    select(Outbox)
                    .join(WorkflowExecution, WorkflowExecution.request_id == Outbox.request_id)
                    .where(Outbox.delivered.is_(False), same_provider)
                    .order_by(Outbox.created_at)
                    .limit(100)
                )
            )
            # Reconstruct nonterminal requests after an application restart.
            active = list(
                s.scalars(
                    select(WorkRequest)
                    .join(WorkflowExecution, WorkflowExecution.request_id == WorkRequest.id)
                    .where(
                        WorkRequest.status.not_in(list(TERMINAL)),
                        WorkflowExecution.engine == self.config.mode,
                    )
                )
            )
            for work in active:
                if work.id not in self.tasks:
                    start = s.scalar(
                        select(Outbox).where(Outbox.request_id == work.id, Outbox.type == "start")
                    )
                    if start:
                        self.start_demo(start.payload)
        for command in commands:
            try:
                if command.type == "start":
                    self.start_demo(command.payload)
                elif command.type == "cancel" and command.request_id in self.runtimes:
                    self.runtimes[command.request_id].cancelled = True
                with self.store.session.begin() as s:
                    saved = s.get(Outbox, command.id)
                    saved.delivered, saved.last_error = True, None
            except Exception as exc:
                self.last_error = type(exc).__name__
                with self.store.session.begin() as s:
                    saved = s.get(Outbox, command.id)
                    saved.attempts += 1
                    saved.last_error = type(exc).__name__
                log.warning(
                    "command_delivery_failed",
                    extra={"command_id": command.id, "error_type": type(exc).__name__},
                )

    def start_demo(self, payload):
        request_id = payload["request_id"]
        if request_id not in self.tasks:
            runtime = DemoRuntime(self.store, self.config, payload)
            self.runtimes[request_id] = runtime
            self.tasks[request_id] = asyncio.create_task(Supervisor(runtime, payload).run())

    def claim_lease(self):
        # Cloud Run revisions can overlap even with max instances set to one.
        # A PostgreSQL session lock elects one local draft dispatcher across revisions.
        if self.config.mode != "anthropic" or self.store.engine.dialect.name != "postgresql":
            return True
        if self.lease is not None:
            self.lease.execute(text("SELECT 1"))
            return True
        connection = self.store.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            if connection.scalar(text("SELECT pg_try_advisory_lock(57199691)")):
                self.lease = connection
                return True
        except Exception:
            connection.invalidate()
            raise
        finally:
            if self.lease is not connection:
                connection.close()
        return False

    def release_lease(self):
        if self.lease is not None:
            # Close the physical connection, not just its pooled wrapper, so a
            # session advisory lock can never leak back into the connection pool.
            self.lease.invalidate()
            self.lease.close()
            self.lease = None

    async def run(self):
        try:
            while True:
                try:
                    if self.claim_lease():
                        await self.tick()
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    await self.close()
                    self.release_lease()
                    log.warning("dispatcher_unavailable", extra={"error_type": type(exc).__name__})
                await asyncio.sleep(1)
        finally:
            await self.close()
            self.release_lease()

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.runtimes.clear()

