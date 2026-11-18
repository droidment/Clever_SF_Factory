import httpx
from fastapi.testclient import TestClient

from scripts import gcp_proxy


def test_proxy_keeps_credentials_server_side_and_blocks_foreign_origins(monkeypatch):
    async def credential():
        return 'test-token'

    observed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: ready\n\n'

    def respond(request):
        observed.append(request)
        return httpx.Response(200, stream=Stream(), headers={'content-type': 'text/event-stream'})

    monkeypatch.setattr(gcp_proxy.credentials, 'get', credential)
    with TestClient(gcp_proxy.app, base_url='http://127.0.0.1:8085') as client:
        original = gcp_proxy.app.state.client
        gcp_proxy.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            assert client.get('/api/config', headers={'Host': 'foreign.example'}).status_code == 403
            assert client.post('/api/requests', headers={'Origin': 'https://foreign.example'}).status_code == 403
            assert client.get('/api/config', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
            response = client.get('/api/events?after=1', headers={'X-Factory-Authorization': 'spoofed', 'Cookie': 'secret=browser'})
            assert response.status_code == 200 and response.content == b'data: ready\n\n'
            assert len(observed) == 1
            outbound = observed[0]
            assert str(outbound.url) == gcp_proxy.UPSTREAM + '/api/events?after=1'
            assert outbound.headers['x-factory-authorization'] == 'Bearer test-token'
            assert outbound.headers['x-serverless-authorization'] == 'Bearer test-token'
            assert 'cookie' not in outbound.headers
            assert 'test-token' not in response.text
            assert 'x-factory-authorization' not in response.headers
        finally:
            client.portal.call(gcp_proxy.app.state.client.aclose)
            gcp_proxy.app.state.client = original
