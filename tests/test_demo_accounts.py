import json
import time

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from factory.api import create_app
from factory.demo_accounts import COOKIE, make_credentials
from factory.store import WorkRequest

PASSWORD = "test-shared-demo-password"


@pytest.fixture
def accounts(config):
    secret = make_credentials(PASSWORD)
    config = config.model_copy(update={"mode": "anthropic", "auth_mode": "google", "public_view": True,
        "anthropic_api_key": SecretStr("fake"), "google_tenant": "test", "google_audience": "test",
        "google_allowed_emails": "owner@example.com", "demo_login_enabled": True,
        "demo_credentials": SecretStr(json.dumps(secret)), "draft_review_required": True})
    app = create_app(config, dispatch=False)
    with TestClient(app, base_url="https://testserver") as client:
        yield app, client, secret


def test_role_handoff_cookies_and_server_enforcement(accounts, work):
    app, client, _ = accounts

    def login(user):
        response = client.post('/api/session/login', json={"username": user, "password": PASSWORD})
        assert response.status_code == 200
        assert "HttpOnly" in response.headers['set-cookie'] and "Secure" in response.headers['set-cookie']
        assert PASSWORD not in response.text
        return client.get('/api/config').json()

    assert login('creator')['roles'] == ['requester', 'viewer']
    result = client.post('/api/requests', headers={'Idempotency-Key': 'role-handoff'}, json=work.model_dump())
    assert result.status_code == 202
    rid = result.json()['request_id']
    with app.state.store.session.begin() as session:
        row = session.get(WorkRequest, rid)
        row.status, row.gate = 'waiting_for_human', 'r0:policy-draft-review'
        assert row.submitted_by == 'demo:creator'
    decision = {'gate': 'r0:policy-draft-review', 'approved': True, 'comment': 'Reviewed draft artifacts'}
    assert client.post(f'/api/requests/{rid}/decisions', json=decision).status_code == 403
    assert client.post(f'/api/requests/{rid}/cancel').status_code == 403
    assert login('viewer')['roles'] == ['viewer']
    assert client.get(f'/api/requests/{rid}').status_code == 200
    assert client.post('/api/requests', headers={'Idempotency-Key': 'viewer-denied'}, json=work.model_dump()).status_code == 403
    assert login('approver')['roles'] == ['approver', 'viewer']
    assert client.post('/api/requests', headers={'Idempotency-Key': 'approver-denied'}, json=work.model_dump()).status_code == 403
    assert client.post(f'/api/requests/{rid}/decisions', json=decision).status_code == 202
    assert client.get(f'/api/requests/{rid}').json()['decisions'][0]['actor'] == 'demo:approver'
    assert 'admin' in login('admin')['roles']
    own = client.post('/api/requests', headers={'Idempotency-Key': 'admin-own-ticket'}, json=work.model_dump()).json()['request_id']
    with app.state.store.session.begin() as session:
        row = session.get(WorkRequest, own)
        row.status, row.gate = 'waiting_for_human', 'r0:policy-draft-review'
    assert client.post(f'/api/requests/{own}/decisions', json=decision).status_code == 403
    assert client.post(f'/api/requests/{own}/cancel').status_code == 202
    assert client.post('/api/session/logout').status_code == 200
    assert client.get('/api/config').json()['actor'] == 'Public visitor'


def test_invalid_login_csrf_expiry_and_forgery(accounts):
    _, client, secret = accounts
    assert client.post('/api/session/login', json={'username': 'admin', 'password': 'wrong'}).status_code == 401
    assert client.post('/api/session/login', json={'username': 'intruder', 'password': PASSWORD}).status_code == 401
    assert client.post('/api/session/login', headers={'Origin': 'https://foreign.example'}, json={'username': 'admin', 'password': PASSWORD}).status_code == 403
    client.cookies.set(COOKIE, 'forged')
    assert client.get('/api/config').status_code == 401
    client.cookies.clear()
    token = jwt.encode({'sub':'admin', 'iss':'factory', 'aud':'factory-demo', 'iat':int(time.time())-100, 'exp':int(time.time())-1}, secret['signing_key'], algorithm='HS256')
    client.cookies.set(COOKIE, token)
    assert client.get('/api/config').status_code == 401
    client.cookies.clear()
    for _ in range(7):
        assert client.post('/api/session/login', json={'username':'admin','password':'wrong'}).status_code == 401
    assert client.post('/api/session/login', json={'username':'admin','password':PASSWORD}).status_code == 429
