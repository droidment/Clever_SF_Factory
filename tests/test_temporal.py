import asyncio
import os

import pytest
from temporalio.worker import Replayer, Worker

from factory.store import Store
from factory.temporal import FactoryWorkflow
from factory.worker import Activities


@pytest.mark.temporal
@pytest.mark.skipif(
    os.environ.get("RUN_TEMPORAL_TESTS") != "1",
    reason="Set RUN_TEMPORAL_TESTS=1 to run the Temporal server integration",
)
async def test_temporal_execution_and_history_replay(config, work, temporal_environment):
    store = Store(config)
    store.create_schema()
    rid, _ = store.intake("demo", "operator", "temporal-integration", work, "demo")
    activities = Activities(store, config)
    payload = {
        "request_id": rid,
        "tenant_id": "demo",
        "work": work.model_dump(mode="json"),
        "simulated": True,
    }
    async with temporal_environment as env:
        async with Worker(
            env.client,
            task_queue="test-factory",
            workflows=[FactoryWorkflow],
            activities=[activities.project, activities.invoke_agent],
        ):
            result = await asyncio.wait_for(
                env.client.execute_workflow(
                    FactoryWorkflow.run, payload, id=f"factory/{rid}", task_queue="test-factory"
                ),
                timeout=45,
            )
            assert result["status"] == "simulated"
            history = await env.client.get_workflow_handle(f"factory/{rid}").fetch_history()
            await Replayer(workflows=[FactoryWorkflow]).replay_workflow(history)


@pytest.mark.temporal
@pytest.mark.skipif(os.environ.get("RUN_TEMPORAL_TESTS") != "1", reason="Set RUN_TEMPORAL_TESTS=1")
async def test_approval_survives_worker_restart(config, work, temporal_environment):
    from sqlalchemy import select

    from factory.dispatch import Dispatcher
    from factory.store import Decision, Outbox, WorkRequest

    work.deploy = True
    store = Store(config)
    store.create_schema()
    rid, _ = store.intake("demo", "operator", "temporal-restart", work, "demo")
    activities = Activities(store, config)
    payload = {
        "request_id": rid,
        "tenant_id": "demo",
        "work": work.model_dump(mode="json"),
        "simulated": True,
    }
    async with temporal_environment as env:
        async with Worker(
            env.client,
            task_queue="restart-test",
            workflows=[FactoryWorkflow],
            activities=[activities.project, activities.invoke_agent],
        ):
            handle = await env.client.start_workflow(
                FactoryWorkflow.run, payload, id=f"factory/{rid}", task_queue="restart-test"
            )
            for _ in range(300):
                with store.session() as s:
                    if s.get(WorkRequest, rid).status == "waiting_for_human":
                        break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("Workflow did not reach approval gate")
        # No worker running: persist the decision and send the durable signal.
        decision = {"gate": "r0:policy-approval", "approved": True, "comment": "Verified after restart"}
        with store.session.begin() as s:
            s.add(Decision(request_id=rid, gate=decision["gate"], actor="reviewer", data=decision))
        await handle.signal(FactoryWorkflow.human_decision, decision)
        # A fresh worker reconstructs state from history and observes the queued signal.
        async with Worker(
            env.client,
            task_queue="restart-test",
            workflows=[FactoryWorkflow],
            activities=[activities.project, activities.invoke_agent],
        ):
            result = await asyncio.wait_for(handle.result(), timeout=30)
            assert result["status"] == "simulated"
            await Replayer(workflows=[FactoryWorkflow]).replay_workflow(await handle.fetch_history())
        with store.session.begin() as s:
            s.add(
                Outbox(request_id=rid, command_key="redelivered-decision", type="decision", payload=decision)
            )
        dispatcher = Dispatcher(store, config.model_copy(update={"mode": "temporal"}))
        dispatcher.client = env.client
        await dispatcher.tick()
        with store.session() as s:
            assert all(command.delivered for command in s.scalars(select(Outbox)))
