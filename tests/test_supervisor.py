import asyncio

import pytest
from sqlalchemy import select

from factory.agents import AgentService, PermanentAgentError
from factory.dispatch import DemoRuntime, Dispatcher
from factory.engine import Supervisor
from factory.store import AgentExecution, Decision, Outbox, Store, WorkRequest


def setup(config, work):
    store = Store(config)
    store.create_schema()
    rid, _ = store.intake("demo", "operator", "engine-request", work, "demo")
    with store.session() as s:
        payload = s.scalar(select(Outbox).where(Outbox.request_id == rid)).payload
    return store, rid, payload


async def test_full_execution_and_retry_cache(config, work):
    store, rid, payload = setup(config, work)
    runtime = DemoRuntime(store, config, payload)
    assert (await Supervisor(runtime, payload).run())["status"] == "simulated"
    with store.session() as s:
        before = list(s.scalars(select(AgentExecution)))
        assert s.get(WorkRequest, rid).status == "simulated"
        assert len(before) == 8
    # Re-execution reuses persisted activity outputs without duplicating projections or artifacts.
    assert (await Supervisor(DemoRuntime(store, config, payload), payload).run())["status"] == "simulated"
    with store.session() as s:
        assert len(list(s.scalars(select(AgentExecution)))) == len(before)


async def test_human_gate_pauses_and_resumes(config, work):
    work.deploy = True
    store, rid, payload = setup(config, work)
    runtime = DemoRuntime(store, config, payload)
    task = asyncio.create_task(Supervisor(runtime, payload).run())
    for _ in range(200):
        await asyncio.sleep(0.01)
        with store.session() as s:
            if s.get(WorkRequest, rid).status == "waiting_for_human":
                break
    with store.session() as s:
        assert s.get(WorkRequest, rid).gate == "r0:policy-approval"
        assert not list(s.scalars(select(AgentExecution).where(AgentExecution.agent == "deployment")))
    with store.session.begin() as s:
        s.add(
            Decision(
                request_id=rid,
                gate="r0:policy-approval",
                actor="reviewer",
                data={"approved": True, "comment": "Evidence reviewed"},
            )
        )
    assert (await asyncio.wait_for(task, 5))["status"] == "simulated"


async def test_failed_check_creates_corrective_work(config, work):
    store, rid, payload = setup(config, work)
    runtime = DemoRuntime(store, config, payload)
    original = runtime.invoke

    async def fail_once(call):
        result = await original(call)
        if call["key"] == "r0:policy-test":
            result = {**result, "passed": False, "findings": ["Regression test failed"]}
        return result

    runtime.invoke = fail_once
    assert (await Supervisor(runtime, payload).run())["status"] == "simulated"
    with store.session() as s:
        work = s.get(WorkRequest, rid)
        assert work.plan["revision"] == 1
        assert work.plan["tasks"][0]["id"] == "corrective-work"


async def test_correction_limit_and_budget(config, work):
    store, _, payload = setup(config, work)
    runtime = DemoRuntime(store, config, payload)
    original = runtime.invoke

    async def always_fail(call):
        result = await original(call)
        return {**result, "passed": False} if call["agent"] == "test" else result

    runtime.invoke = always_fail
    result = await Supervisor(runtime, payload).run()
    assert result["status"] == "failed"
    assert "Correction limit" in result["error"]
    payload["work"]["budget_usd"] = 1
    result = await Supervisor(DemoRuntime(store, config, payload), payload).run()
    assert "Budget insufficient" in result["error"]


async def test_cancellation_at_gate(config, work):
    work.acceptance_criteria = []
    store, _, payload = setup(config, work)
    runtime = DemoRuntime(store, config, payload)
    task = asyncio.create_task(Supervisor(runtime, payload).run())
    await asyncio.sleep(0.1)
    runtime.cancelled = True
    assert (await asyncio.wait_for(task, 2))["status"] == "cancelled"


async def test_deployment_cannot_be_called_without_approval(config, work):
    store, rid, payload = setup(config, work)
    with pytest.raises(PermanentAgentError, match="approved decision"):
        await AgentService(store, config).invoke(
            {**payload, "agent": "deployment", "key": "r0:deploy", "release_gate": "r0:policy-approval"}
        )


async def test_outbox_recovery_after_start(config, work):
    store, rid, _ = setup(config, work)
    dispatcher = Dispatcher(store, config)
    await dispatcher.tick()
    await asyncio.gather(*dispatcher.tasks.values())
    with store.session() as s:
        assert s.scalar(select(Outbox).where(Outbox.request_id == rid)).delivered
        assert s.get(WorkRequest, rid).status == "simulated"
    await dispatcher.close()
