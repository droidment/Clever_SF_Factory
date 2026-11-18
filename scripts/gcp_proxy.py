"""Loopback-only forwarding to this private cloud workspace; credentials stay in RAM."""

import asyncio
import os
import shutil
import subprocess
import time
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

UPSTREAM = 'https://factory-drafts-anpmlvlena-uc.a.run.app'
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade'}
ORIGINS = {'http://127.0.0.1:8085', 'http://localhost:8085'}


class Credentials:
    def __init__(self):
        self.token = ''
        self.updated = 0
        self.lock = asyncio.Lock()

    async def get(self):
        async with self.lock:
            if time.monotonic() - self.updated > 300 or not self.token:
                cli = shutil.which('gcloud.cmd') or shutil.which('gcloud')
                self.token = (await asyncio.to_thread(
                    subprocess.check_output,
                    # Uses the CLI's active account unless FACTORY_GCLOUD_ACCOUNT names another.
                    [cli, 'auth', 'print-identity-token',
                     *([f'--account={account}'] if (account := os.environ.get('FACTORY_GCLOUD_ACCOUNT')) else [])],
                    text=True, stderr=subprocess.DEVNULL, timeout=30,
                )).strip()
                self.updated = time.monotonic()
            return self.token


credentials = Credentials()


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=httpx.Timeout(330, connect=20), follow_redirects=False) as client:
        app.state.client = client
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'])
async def forward(path: str, request: Request):
    if request.headers.get('host') not in {'127.0.0.1:8085', 'localhost:8085'}:
        return Response('Invalid local host', status_code=403)
    origin = request.headers.get('origin')
    if (origin and origin not in ORIGINS) or request.headers.get('sec-fetch-site') == 'cross-site':
        return Response('Origin not allowed', status_code=403)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 128000:
            return Response('Request too large', status_code=413)
    try:
        token = await credentials.get()
    except (subprocess.SubprocessError, OSError):
        return Response('Google credentials unavailable. Run gcloud auth login, then retry.', status_code=503)
    excluded = HOP_HEADERS | {'host', 'authorization', 'x-serverless-authorization',
                              'x-factory-authorization', 'cookie', 'accept-encoding', 'content-length'}
    headers = {key: value for key, value in request.headers.items()
               if key.lower() not in excluded and not key.lower().startswith(('x-forwarded-', 'x-goog-'))}
    headers.update({'X-Serverless-Authorization': 'Bearer ' + token,
                    'X-Factory-Authorization': 'Bearer ' + token, 'Accept-Encoding': 'identity'})
    session_cookie = request.cookies.get('factory_demo_session')
    if session_cookie and all(char.isalnum() or char in '._-' for char in session_cookie):
        headers['Cookie'] = 'factory_demo_session=' + session_cookie
    # Append an absolute path to a fixed origin; never accept a client-selected upstream.
    url = UPSTREAM + request.url.path
    if request.url.query:
        url += '?' + request.url.query
    client = request.app.state.client
    try:
        upstream = await client.send(client.build_request(request.method, url, headers=headers, content=bytes(body)), stream=True)
    except httpx.HTTPError:
        return Response('Cloud workspace is unavailable.', status_code=502)
    response_headers = {key: value for key, value in upstream.headers.items()
                        if key.lower() not in HOP_HEADERS | {'authorization', 'x-factory-authorization', 'x-serverless-authorization'}}
    return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                             headers=response_headers, background=BackgroundTask(upstream.aclose))


if __name__ == '__main__':
    print('Cloud workspace: http://127.0.0.1:8085/ (close this process to stop forwarding)', flush=True)
    uvicorn.run(app, host='127.0.0.1', port=8085, access_log=False, proxy_headers=False)
