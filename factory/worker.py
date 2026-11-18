"""Standalone worker for the Postgres runtime. Run as many of these as the workload needs."""

import asyncio
import contextlib
import logging

from factory.config import settings
from factory.durable import DurableWorker
from factory.lifecycle import shutdown_event
from factory.store import Store


async def main():
    config = settings()
    if config.runtime != "postgres":
        raise RuntimeError("Set FACTORY_RUNTIME=postgres before starting workers")
    store = Store(config)
    worker = DurableWorker(store, config)
    try:
        with shutdown_event() as stopping:
            task = asyncio.create_task(worker.run())
            logging.info("Worker %s started", worker.worker_id)
            try:
                await stopping.wait()
            finally:
                # Running requests are released, not failed; another worker resumes them.
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
    finally:
        store.engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
