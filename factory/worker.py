import asyncio
import contextlib
import logging
from datetime import timedelta

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from factory.agents import AgentService
from factory.config import settings
from factory.lifecycle import shutdown_event
from factory.store import Store
from factory.temporal import FactoryWorkflow


class Activities:
    def __init__(self, store, config):
        self.store, self.agents = store, AgentService(store, config)

    @activity.defn(name="project")
    async def project(self, payload: dict):
        await asyncio.to_thread(
            self.store.project,
            payload["request_id"],
            payload["tenant_id"],
            payload["key"],
            payload["type_"],
            payload["message"],
            payload["data"],
        )

    @activity.defn(name="invoke_agent")
    async def invoke_agent(self, payload: dict) -> dict:
        async def heartbeat():
            while True:
                activity.heartbeat({"agent": payload["agent"], "key": payload["key"]})
                await asyncio.sleep(5)

        pulse = asyncio.create_task(heartbeat())
        try:
            return await self.agents.invoke(payload, activity.info().attempt)
        finally:
            pulse.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pulse


async def connect(config):
    return await Client.connect(
        config.temporal_address,
        namespace=config.temporal_namespace,
        api_key=config.temporal_api_key or None,
        tls=config.temporal_tls,
    )


async def main():
    config = settings()
    if config.mode != "temporal":
        raise RuntimeError("Set FACTORY_MODE=temporal and configure the gateway before starting workers")
    store = Store(config)
    try:
        with shutdown_event() as stopping:
            activities = Activities(store, config)
            client = await connect(config)
            async with Worker(
                client,
                task_queue=config.task_queue,
                workflows=[FactoryWorkflow],
                activities=[activities.project, activities.invoke_agent],
                max_concurrent_activities=32,
                graceful_shutdown_timeout=timedelta(seconds=8),
            ):
                logging.info("Worker polling %s", config.task_queue)
                await stopping.wait()
    finally:
        store.engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
