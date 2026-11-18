"""Run with `python -m gateway`. Binds to loopback unless GATEWAY_HOST says otherwise."""

import os

import uvicorn

from gateway.app import create_app

if __name__ == "__main__":
    uvicorn.run(
        create_app(),
        host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("GATEWAY_PORT", "8100")),
        log_level="info",
    )
