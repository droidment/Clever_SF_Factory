"""Portable HTTP entrypoint: Docker defaults to 8000; Cloud Run supplies PORT."""

import os

import uvicorn


def server_options(environ=None):
    environ = os.environ if environ is None else environ
    try:
        port = int(environ.get("PORT", "8000"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError("PORT must be an integer between 1 and 65535") from None
    return {
        "host": "0.0.0.0",
        "port": port,
        "proxy_headers": True,
        # Cloud Run terminates TLS before forwarding to the container. Outside that
        # managed ingress boundary, only trust the local reverse proxy by default.
        "forwarded_allow_ips": "*" if environ.get("K_SERVICE") else "127.0.0.1",
        "timeout_graceful_shutdown": 8,
    }


if __name__ == "__main__":
    uvicorn.run("factory.api:app", **server_options())
