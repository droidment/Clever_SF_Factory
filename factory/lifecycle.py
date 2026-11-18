"""Translate container termination into an orderly asyncio shutdown."""

import asyncio
import signal
from contextlib import contextmanager


@contextmanager
def shutdown_event():
    loop = asyncio.get_running_loop()
    event = asyncio.Event()
    previous = {}

    def stop(signum, frame):
        loop.call_soon_threadsafe(event.set)

    try:
        # signal.signal also works with the Windows Proactor loop.
        for name in (signal.SIGTERM, signal.SIGINT):
            previous[name] = signal.signal(name, stop)
        yield event
    finally:
        for name, handler in previous.items():
            signal.signal(name, handler)
