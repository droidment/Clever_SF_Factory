import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import SecretStr

from factory.api import create_app
from factory.dispatch import Dispatcher
from factory.store import Store


@pytest.mark.parametrize("public_view", [False, True])
def test_google_auth_requires_valid_token_and_explicit_account(config, work, public_view):
    config = config.model_copy(
        update={
            "mode": "anthropic",
            "auth_mode": "google",
            "anthropic_api_key": SecretStr("not-a-real-key"),
            "google_audience": "test-client",
            "google_allowed_emails": "owner@example.com",
            "google_tenant": "cloud-test",
            "trusted_origins": "http://127.0.0.1:8085",
            "public_view": public_view,
        }
    )
    app = create_app(config, dispatch=False)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    app.state.jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key())
    )

    def headers(**overrides):
        claims = {
            "iss": "https://accounts.google.com",
            "aud": "test-client",
            "sub": "google-id",
            "email": "owner@example.com",
            "email_verified": True,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            **overrides,
        }
        return {"Authorization": "Bearer " + jwt.encode(claims, key, algorithm="RS256")}

    with TestClient(app) as client:
        assert client.get("/api/config").status_code == (200 if public_view else 401)
        assert (
            client.get(
                "/api/config", headers={"X-Goog-Authenticated-User-Email": "owner@example.com"}
            ).status_code
            == (200 if public_view else 401)
        )
        valid = client.get("/api/config", headers=headers())
        assert valid.status_code == 200 and valid.json()["tenant"] == "cloud-test"
        assert valid.json()["actor"] == "owner@example.com"
        proxy_header = {"X-Factory-Authorization": headers()["Authorization"]}
        assert client.get("/api/config", headers=proxy_header).status_code == 200
        assert client.get("/api/config", headers={"X-Factory-Authorization": "Bearer forged"}).status_code == 401
        for changes in (
            {"aud": "wrong"},
            {"email_verified": False},
            {"exp": 1},
            {"iss": "https://fake.example"},
        ):
            assert client.get("/api/config", headers=headers(**changes)).status_code == 401
            assert client.get("/api/config", headers={"X-Factory-Authorization": headers(**changes)["Authorization"]}).status_code == 401
        assert client.get("/api/config", headers=headers(email="intruder@example.com")).status_code == 403
        request_headers = {**headers(), "Origin": "http://127.0.0.1:8085", "Idempotency-Key": "cloud-request"}
        assert (
            client.post("/api/requests", headers=request_headers, json=work.model_dump()).status_code == 202
        )
        request_headers["Origin"] = "https://untrusted.example"
        assert (
            client.post("/api/requests", headers=request_headers, json=work.model_dump()).status_code == 403
        )
        if public_view:
            guest = client.get("/api/config").json()
            assert guest["roles"] == ["viewer"] and guest["actor"] == "Public visitor"
            records = client.get("/api/requests").json()
            request_id = records["items"][0]["id"]
            assert client.get(f"/api/requests/{request_id}").status_code == 200
            assert client.post("/api/requests", headers={"Idempotency-Key": "guest-request"}, json=work.model_dump()).status_code == 403
            assert client.post(f"/api/requests/{request_id}/cancel").status_code == 403
            assert client.post(f"/api/requests/{request_id}/decisions", json={"gate": "approval", "approved": True, "comment": "Guest must be denied"}).status_code == 403
            assert client.get("/metrics").status_code == 403


async def test_postgres_elects_one_draft_dispatcher(config):
    if not config.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL advisory-lock integration runs in compose.test.yaml")
    config = config.model_copy(update={"mode": "anthropic", "anthropic_api_key": SecretStr("fake-key")})
    first_store, second_store = Store(config), Store(config)
    first, second = Dispatcher(first_store, config), Dispatcher(second_store, config)
    try:
        assert first.claim_lease()
        assert not second.claim_lease()
        first.release_lease()
        assert second.claim_lease()
        assert not first.claim_lease()
    finally:
        first.release_lease()
        second.release_lease()
        first_store.engine.dispose()
        second_store.engine.dispose()
