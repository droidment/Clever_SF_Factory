"""Durable execution on PostgreSQL alone: no workflow service, no in-memory state that matters.

A request is run by replaying the supervisor from the start. Everything the supervisor
has already done is in the database, so a replay skips it: recorded events are not written
again, finished agent calls return their stored result, and human decisions are read from
the decisions table. Whatever is left is the work that still has to happen.

A worker owns a request through a lease it keeps renewing. If the worker stops, the lease
expires and any other worker picks the request up and replays it. A request waiting for a
person holds no lease and no memory; the command that carries the decision wakes it.
"""

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError

from factory.agents import AgentService, PermanentAgentError
from factory.contracts import TERMINAL
from factory.engine import Supervisor
from factory.store import (
    AgentExecution,
    Decision,
    Event,
    ExecutionLease,
    Outbox,
    WorkflowExecution,
    WorkRequest,
    now,
)

log = logging.getLogger("factory.durable")

WORKER_LOST = "Worker stopped before the attempt finished"


class Interrupt(BaseException):
    """Stops a run without failing the request. Not an Exception, so the supervisor lets it pass."""


class Suspend(Interrupt):
    """The request is waiting for a person."""


class RetryLater(Interrupt):
    """The database was unreachable; the request is retried after a delay."""


class DurableRuntime:
    def __init__(self, store, config, payload, *, cancelled=False, seen=(), retry_base=2.0):
        self.store, self.config, self.payload = store, config, payload
        self.request_id, self.tenant_id = payload["request_id"], payload["tenant_id"]
        self.cancelled = cancelled
        self.seen = set(seen)
        self.retry_base = retry_base
        self.agents = AgentService(store, config)
        # Draft calls are billed; a failed one is surfaced rather than silently repeated.
        self.max_attempts = 1 if config.mode == "anthropic" else 3

    async def _db(self, fn, *args):
        for attempt in range(3):
            try:
                return await asyncio.to_thread(fn, *args)
            except DBAPIError as exc:
                if attempt == 2:
                    raise RetryLater(type(exc).__name__) from exc
                await asyncio.sleep(0.2 * (attempt + 1))

    async def emit(self, key, type_, message, data):
        if key in self.seen:
            return
        await self._db(self.store.project, self.request_id, self.tenant_id, key, type_, message, data)
        self.seen.add(key)

    def _attempts(self, key):
        with self.store.session.begin() as s:
            rows = list(
                s.scalars(
                    select(AgentExecution).where(
                        AgentExecution.request_id == self.request_id, AgentExecution.execution_key == key
                    )
                )
            )
            for row in rows:
                # Only the lease holder runs a request, so an attempt still marked running was abandoned.
                if row.status == "running":
                    row.status, row.error, row.completed_at = "failed", WORKER_LOST, now()
            return (
                any(row.status == "completed" for row in rows),
                sum(row.status == "failed" and row.error != WORKER_LOST for row in rows),
                max((row.attempt for row in rows), default=0),
            )

    async def invoke(self, payload):
        completed, failures, attempt = await self._db(self._attempts, payload["key"])
        if completed:
            return await self.agents.invoke(payload, attempt)
        if failures >= self.max_attempts:
            raise PermanentAgentError("Retry policy exhausted before this run")
        while True:
            attempt += 1
            try:
                return await asyncio.wait_for(
                    self.agents.invoke(payload, attempt), self.config.agent_timeout_seconds
                )
            except (ValueError, PermanentAgentError):
                raise
            except Exception:
                failures += 1
                if failures >= self.max_attempts:
                    raise
                await asyncio.sleep(self.retry_base * 2 ** (failures - 1))

    def _decision(self, gate):
        with self.store.session() as s:
            found = s.scalar(
                select(Decision).where(Decision.request_id == self.request_id, Decision.gate == gate)
            )
            return found.data if found else None

    async def decision(self, gate):
        found = await self._db(self._decision, gate)
        if found is not None:
            return found
        if self.cancelled:
            return {"approved": False, "comment": "Cancelled"}
        raise Suspend(gate)


class DurableWorker:
    """Claims runnable requests and executes them. Any number of these may run against one database."""

    def __init__(self, store, config, *, worker_id=None, poll_seconds=0.5, retry_base=2.0, max_claims=10):
        self.store, self.config = store, config
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.poll_seconds, self.retry_base, self.max_claims = poll_seconds, retry_base, max_claims
        self.tasks, self.runtimes = {}, {}
        self.last_error = None
        self.renewed = time.monotonic()

    # --- database steps (synchronous; called through a thread) ---

    def pump(self):
        """Turn delivered commands into wake-ups, and adopt requests that have no lease yet."""
        current = now()
        with self.store.session.begin() as s:
            commands = list(
                s.scalars(
                    select(Outbox)
                    .join(WorkflowExecution, WorkflowExecution.request_id == Outbox.request_id)
                    # Never run a request accepted under one provider with another.
                    .where(Outbox.delivered.is_(False), WorkflowExecution.engine == self.config.mode)
                    .order_by(Outbox.created_at)
                    .limit(100)
                    .with_for_update(skip_locked=True, of=Outbox)
                )
            )
            for command in commands:
                work = s.get(WorkRequest, command.request_id)
                if work.status not in TERMINAL:
                    lease = s.get(ExecutionLease, command.request_id, with_for_update=True)
                    if lease is None:
                        lease = ExecutionLease(
                            request_id=command.request_id, signals=0, claims=0, cancel_requested=False
                        )
                        s.add(lease)
                    lease.signals += 1
                    lease.wake_at = current
                    if command.type == "cancel":
                        lease.cancel_requested = True
                command.delivered, command.last_error = True, None
            s.flush()
            # Requests started by the in-process runtime before this one was switched on.
            orphans = list(
                s.scalars(
                    select(WorkRequest.id)
                    .join(WorkflowExecution, WorkflowExecution.request_id == WorkRequest.id)
                    .join(Outbox, Outbox.request_id == WorkRequest.id)
                    .outerjoin(ExecutionLease, ExecutionLease.request_id == WorkRequest.id)
                    .where(
                        WorkflowExecution.engine == self.config.mode,
                        WorkRequest.status.not_in(list(TERMINAL)),
                        Outbox.type == "start",
                        Outbox.delivered.is_(True),
                        ExecutionLease.request_id.is_(None),
                    )
                    .limit(100)
                )
            )
            for request_id in orphans:
                s.add(
                    ExecutionLease(
                        request_id=request_id, wake_at=current, signals=0, claims=0, cancel_requested=False
                    )
                )

    def claim(self):
        current = now()
        with self.store.session.begin() as s:
            query = (
                select(ExecutionLease)
                .join(WorkflowExecution, WorkflowExecution.request_id == ExecutionLease.request_id)
                .where(
                    WorkflowExecution.engine == self.config.mode,
                    ExecutionLease.wake_at.is_not(None),
                    ExecutionLease.wake_at <= current,
                    or_(ExecutionLease.lease_until.is_(None), ExecutionLease.lease_until < current),
                )
                .order_by(ExecutionLease.wake_at)
                .limit(1)
                # Two workers never wait on, or take, the same request.
                .with_for_update(skip_locked=True, of=ExecutionLease)
            )
            if self.tasks:
                query = query.where(ExecutionLease.request_id.not_in(list(self.tasks)))
            lease = s.scalar(query)
            if lease is None:
                return None
            work = s.get(WorkRequest, lease.request_id)
            start = s.scalar(
                select(Outbox).where(Outbox.request_id == lease.request_id, Outbox.type == "start")
            )
            if work.status in TERMINAL or start is None:
                s.delete(lease)
                return {"skip": True}
            lease.owner = self.worker_id
            lease.lease_until = current + timedelta(seconds=self.config.lease_seconds)
            lease.claims += 1
            return {
                "request_id": lease.request_id,
                "tenant_id": work.tenant_id,
                "payload": start.payload,
                "signals": lease.signals,
                "cancelled": lease.cancel_requested,
                "exhausted": lease.claims > self.max_claims,
                "seen": list(s.scalars(select(Event.event_key).where(Event.request_id == lease.request_id))),
            }

    def renew(self):
        current = now()
        with self.store.session.begin() as s:
            held = {}
            for lease in s.scalars(
                select(ExecutionLease).where(ExecutionLease.owner == self.worker_id).with_for_update()
            ):
                if lease.request_id in self.tasks:
                    lease.lease_until = current + timedelta(seconds=self.config.lease_seconds)
                    held[lease.request_id] = lease.cancel_requested
                else:
                    lease.owner, lease.lease_until = None, None
            return held

    def release(self, request_id, outcome, signals=None):
        with self.store.session.begin() as s:
            lease = s.get(ExecutionLease, request_id, with_for_update=True)
            if lease is None or lease.owner != self.worker_id:
                return
            if outcome == "done":
                s.delete(lease)
                return
            lease.owner, lease.lease_until = None, None
            if outcome == "parked":
                # A command that arrived during this run has not been seen by it; run once more.
                lease.claims = 0
                lease.wake_at = now() if lease.signals != signals else None
            elif outcome == "retry":
                delay = min(300.0, self.retry_base * 2 ** min(lease.claims, 10))
                lease.wake_at = now() + timedelta(seconds=delay)
            else:
                # Orderly hand-over (shutdown): another worker may resume at once.
                lease.claims = max(0, lease.claims - 1)

    def abandon(self, claimed):
        self.store.project(
            claimed["request_id"],
            claimed["tenant_id"],
            "runtime-exhausted",
            "workflow.status",
            "Execution stopped: workers failed repeatedly while running this request",
            {"status": "failed"},
        )
        self.release(claimed["request_id"], "done")

    # --- execution ---

    async def execute(self, claimed):
        request_id = claimed["request_id"]
        runtime = DurableRuntime(
            self.store,
            self.config,
            claimed["payload"],
            cancelled=claimed["cancelled"],
            seen=claimed["seen"],
            retry_base=self.retry_base,
        )
        self.runtimes[request_id] = runtime
        outcome = "retry"
        try:
            await Supervisor(runtime, claimed["payload"]).run()
            outcome = "done"
        except Suspend:
            outcome = "parked"
        except asyncio.CancelledError:
            outcome = "released"
            raise
        except BaseException as exc:
            log.warning(
                "run_interrupted", extra={"request_id": request_id, "error_type": type(exc).__name__}
            )
        finally:
            self.tasks.pop(request_id, None)
            self.runtimes.pop(request_id, None)
            # Not awaited: this must complete even while the task is being cancelled.
            with contextlib.suppress(Exception):
                self.release(request_id, outcome, claimed["signals"])

    async def heartbeat(self):
        held = await asyncio.to_thread(self.renew)
        self.renewed = time.monotonic()
        for request_id, task in list(self.tasks.items()):
            if request_id not in held:
                # The lease was lost; another worker may already be running this request.
                task.cancel()
            elif held[request_id] and request_id in self.runtimes:
                self.runtimes[request_id].cancelled = True

    async def tick(self):
        self.last_error = None
        await asyncio.to_thread(self.pump)
        while len(self.tasks) < self.config.worker_concurrency:
            claimed = await asyncio.to_thread(self.claim)
            if claimed is None:
                break
            if claimed.get("skip"):
                continue
            if claimed["exhausted"]:
                await asyncio.to_thread(self.abandon, claimed)
                continue
            self.tasks[claimed["request_id"]] = asyncio.create_task(self.execute(claimed))
        if time.monotonic() - self.renewed >= self.config.lease_seconds / 3:
            await self.heartbeat()

    async def run(self):
        self.renewed = time.monotonic()
        try:
            while True:
                try:
                    await self.tick()
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    log.warning("worker_tick_failed", extra={"error_type": type(exc).__name__})
                    if self.tasks and time.monotonic() - self.renewed > self.config.lease_seconds:
                        # Leases could not be renewed in time; stop before another worker takes over.
                        await self.close()
                await asyncio.sleep(self.poll_seconds)
        finally:
            await self.close()

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.runtimes.clear()
