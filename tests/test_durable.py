"""The Postgres runtime. These run on SQLite by default and on PostgreSQL when
FACTORY_TEST_DATABASE_URL is set; only PostgreSQL exercises real row locking."""

import asyncio
from collections import Counter
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from factory.agents import AgentService
from factory.api import create_app
from factory.config import Settings
from factory.durable import WORKER_LOST, DurableWorker
from factory.store import (
    AgentExecution,
    Decision,
    Event,
    ExecutionLease,
    Outbox,
    Store,
    WorkRequest,
    now,
)

GATE = "r0:policy-approval"


def make_store(config):
    store = Store(config)
    store.create_schema()
    return store


def worker(store, config, name):
    return DurableWorker(store, config, worker_id=name, retry_base=0.01)


async def drain(*workers):
    """Run until no worker has anything left to claim."""
    for _ in range(50):
        await asyncio.gather(*(w.tick() for w in workers))
        running = [task for w in workers for task in w.tasks.values()]
        if not running:
            return
        await asyncio.gather(*running, return_exceptions=True)
    pytest.fail("Workers did not become idle")


def status(store, rid):
    with store.session() as s:
        return s.get(WorkRequest, rid).status


def lease(store, rid):
    with store.session() as s:
        return s.get(ExecutionLease, rid)


def decide(store, rid, approved=True):
    data = {"gate": GATE, "approved": approved, "comment": "Evidence reviewed"}
    with store.session.begin() as s:
        s.add(Decision(request_id=rid, gate=GATE, actor="reviewer", data=data))
        s.add(Outbox(request_id=rid, command_key=f"decision:{GATE}", type="decision", payload=data))


def attempts(store, rid):
    with store.session() as s:
        return list(s.scalars(select(AgentExecution).where(AgentExecution.request_id == rid)))


async def test_request_runs_to_completion_and_leaves_no_lease(config, work):
    store = make_store(config)
    rid, _ = store.intake("demo", "operator", "durable-1", work, "demo")
    await drain(worker(store, config, "a"))
    assert status(store, rid) == "simulated"
    assert lease(store, rid) is None
    with store.session() as s:
        assert all(command.delivered for command in s.scalars(select(Outbox)))
    assert all(a.status == "completed" for a in attempts(store, rid))


async def test_waiting_request_holds_nothing_and_another_worker_resumes_it(config, work):
    work.deploy = True
    store = make_store(config)
    rid, _ = store.intake("demo", "operator", "durable-gate", work, "demo")
    first = worker(store, config, "a")
    await drain(first)
    assert status(store, rid) == "waiting_for_human"
    parked = lease(store, rid)
    assert parked.owner is None and parked.wake_at is None and not first.tasks
    before = Counter(a.execution_key for a in attempts(store, rid))
    with store.session() as s:
        events_before = len(list(s.scalars(select(Event).where(Event.request_id == rid))))

    # Nothing wakes it until a decision arrives.
    await drain(first)
    assert status(store, rid) == "waiting_for_human"

    # The first worker is gone; a different one picks the request up from the database alone.
    decide(store, rid)
    await drain(worker(store, config, "b"))
    assert status(store, rid) == "simulated"
    after = Counter(a.execution_key for a in attempts(store, rid))
    assert all(count == 1 for count in after.values())
    assert all(after[key] == 1 for key in before)
    assert set(after) - set(before)  # work behind the gate ran only after approval
    with store.session() as s:
        keys = [e.event_key for e in s.scalars(select(Event).where(Event.request_id == rid))]
    assert len(keys) == len(set(keys)) > events_before


async def test_request_survives_a_worker_that_dies_mid_call(config, work):
    store = make_store(config)
    rid, _ = store.intake("demo", "operator", "durable-crash", work, "demo")
    slow = config.model_copy(update={"demo_delay": 3600})
    dying = worker(store, slow, "dying")
    await dying.tick()
    for _ in range(200):
        if attempts(store, rid):
            break
        await asyncio.sleep(0.01)
    # Hard stop: the worker gets no chance to release its lease or close its attempt.
    dying.release = lambda *args, **kwargs: None
    await dying.close()
    with store.session.begin() as s:
        s.scalar(select(AgentExecution).where(AgentExecution.request_id == rid)).status = "running"
    assert lease(store, rid).owner == "dying"

    survivor = worker(store, config, "survivor")
    await drain(survivor)
    assert status(store, rid) == "triaging"  # still leased to the dead worker

    with store.session.begin() as s:
        s.get(ExecutionLease, rid).lease_until = now() - timedelta(seconds=1)
    await drain(survivor)
    assert status(store, rid) == "simulated"
    triage = sorted((a for a in attempts(store, rid) if a.agent == "triage"), key=lambda a: a.attempt)
    assert [(a.attempt, a.status) for a in triage] == [(1, "failed"), (2, "completed")]
    assert triage[0].error == WORKER_LOST


async def test_lost_lease_stops_the_old_worker(config, work):
    store = make_store(config)
    rid, _ = store.intake("demo", "operator", "durable-fence", work, "demo")
    slow = config.model_copy(update={"demo_delay": 3600})
    old = worker(store, slow, "old")
    await old.tick()
    task = old.tasks[rid]
    with store.session.begin() as s:
        s.get(ExecutionLease, rid).owner = "new"
    await old.heartbeat()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and not old.tasks
    assert lease(store, rid).owner == "new"  # the old worker did not touch a lease it no longer holds


async def test_cancel_reaches_a_parked_request(config, work):
    work.deploy = True
    store = make_store(config)
    rid, _ = store.intake("demo", "operator", "durable-cancel", work, "demo")
    runner = worker(store, config, "a")
    await drain(runner)
    assert status(store, rid) == "waiting_for_human"
    with store.session.begin() as s:
        s.add(Outbox(request_id=rid, command_key="cancel", type="cancel", payload={}))
    await drain(runner)
    assert status(store, rid) == "cancelled"
    assert lease(store, rid) is None


async def test_two_workers_share_requests_without_duplicating_work(config, work):
    store, other = make_store(config), Store(config)
    try:
        ids = [store.intake("demo", "operator", f"durable-many-{i}", work, "demo")[0] for i in range(6)]
        await drain(worker(store, config, "a"), worker(other, config, "b"))
        for rid in ids:
            assert status(store, rid) == "simulated"
            keys = Counter(a.execution_key for a in attempts(store, rid))
            assert len(keys) == 8 and set(keys.values()) == {1}
    finally:
        other.engine.dispose()


async def test_failed_agent_call_is_retried_and_recorded(config, work, monkeypatch):
    store = make_store(config)
    rid, _ = store.intake("demo", "operator", "durable-retry", work, "demo")
    original, failed = AgentService.simulate, []

    def flaky(self, name, work_input, payload):
        if name == "test" and not failed:
            failed.append(payload["key"])
            raise RuntimeError("sandbox unavailable")
        return original(self, name, work_input, payload)

    monkeypatch.setattr(AgentService, "simulate", flaky)
    await drain(worker(store, config, "a"))
    assert status(store, rid) == "simulated"
    tried = sorted((a for a in attempts(store, rid) if a.execution_key == failed[0]), key=lambda a: a.attempt)
    assert [(a.attempt, a.status) for a in tried] == [(1, "failed"), (2, "completed")]


async def test_database_outage_delays_the_request_instead_of_failing_it(config, work, monkeypatch):
    store = make_store(config)
    rid, _ = store.intake("demo", "operator", "durable-outage", work, "demo")
    original = store.project

    def unavailable(*args):
        raise OperationalError("select 1", {}, Exception("connection refused"))

    monkeypatch.setattr(store, "project", unavailable)
    runner = worker(store, config, "a")
    await drain(runner)
    held = lease(store, rid)
    assert status(store, rid) == "queued" and held.owner is None and held.claims == 1
    assert held.wake_at is not None

    monkeypatch.setattr(store, "project", original)
    with store.session.begin() as s:
        s.get(ExecutionLease, rid).wake_at = now() - timedelta(seconds=1)
    await drain(runner)
    assert status(store, rid) == "simulated"


async def test_request_that_keeps_stopping_workers_is_failed(config, work):
    store = make_store(config)
    rid, _ = store.intake("demo", "operator", "durable-poison", work, "demo")
    runner = worker(store, config, "a")
    await asyncio.to_thread(runner.pump)
    with store.session.begin() as s:
        s.get(ExecutionLease, rid).claims = runner.max_claims
    await drain(runner)
    assert status(store, rid) == "failed"
    assert lease(store, rid) is None


async def test_requests_from_the_in_process_runtime_are_adopted(config, work):
    work.deploy = True
    store = make_store(config)
    rid, _ = store.intake("demo", "operator", "durable-adopt", work, "demo")
    with store.session.begin() as s:
        s.scalar(select(Outbox).where(Outbox.request_id == rid)).delivered = True
    await drain(worker(store, config, "a"))
    assert status(store, rid) == "waiting_for_human"


def test_api_runs_requests_on_the_postgres_runtime(config, work):
    app = create_app(config.model_copy(update={"runtime": "postgres"}))
    with TestClient(app) as client:
        assert client.get("/healthz").json()["runtime"] == "postgres"
        response = client.post("/api/requests", headers={"Idempotency-Key": "durable-api"}, json=work.model_dump())
        rid = response.json()["request_id"]
        for _ in range(200):
            if client.get(f"/api/requests/{rid}").json()["status"] == "simulated":
                break
            import time

            time.sleep(0.05)
        else:
            pytest.fail("Request did not finish on the Postgres runtime")
        assert client.get("/readyz").json()["dispatch_error"] is None


def test_runtime_settings_fail_closed():
    with pytest.raises(ValueError, match="PostgreSQL"):
        Settings(runtime="postgres", _env_file=None)
    with pytest.raises(ValueError, match="durable Postgres runtime"):
        Settings(
            mode="gateway",
            auth_mode="oidc",
            gateway_url="https://gateway.example/execute",
            oidc_issuer="https://identity.example/",
            oidc_jwks_url="https://identity.example/jwks",
            _env_file=None,
        )
