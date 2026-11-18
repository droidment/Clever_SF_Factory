import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from factory.config import Settings


@pytest.fixture
def identity(app, client):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    app.state.config.auth_mode = "oidc"
    app.state.config.oidc_issuer = "https://identity.example/"
    app.state.jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key())
    )

    def token(**overrides):
        claims = {
            "iss": "https://identity.example/",
            "aud": "software-factory",
            "sub": "engineer-17",
            "tid": "team-1",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "roles": ["viewer", "requester", "approver"],
            "repositories": ["platform/order-service"],
            **overrides,
        }
        return jwt.encode(claims, key, algorithm="RS256")

    return token


def test_valid_signed_identity_and_invalid_claims(client, identity):
    assert client.get("/api/config").status_code == 401
    valid = client.get("/api/config", headers={"Authorization": f"Bearer {identity()}"})
    assert valid.status_code == 200
    assert valid.json()["tenant"] == "team-1"
    for overrides in [
        {"aud": "wrong-api"},
        {"iss": "https://other.example/"},
        {"exp": int(time.time()) - 10},
        {"roles": "admin"},
        {"tid": 123},
    ]:
        assert (
            client.get(
                "/api/config", headers={"Authorization": f"Bearer {identity(**overrides)}"}
            ).status_code
            == 401
        )


def test_forged_signature_rejected(client, identity):
    token = identity()
    forged = token.rsplit(".", 1)[0] + ".invalid-signature"
    assert client.get("/api/config", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_release_requires_separate_approver(client, app, identity, work):
    headers = {"Authorization": f"Bearer {identity()}", "Idempotency-Key": "auth-separation-test"}
    response = client.post("/api/requests", headers=headers, json=work.model_dump(mode="json"))
    rid = response.json()["request_id"]
    app.state.store.project(
        rid,
        "team-1",
        "wait",
        "workflow.status",
        "Authorize",
        {"status": "waiting_for_human", "gate": "r0:policy-approval"},
    )
    decision = {"gate": "r0:policy-approval", "approved": True, "comment": "Reviewed evidence"}
    assert client.post(f"/api/requests/{rid}/decisions", headers=headers, json=decision).status_code == 403
    reviewer = {"Authorization": f"Bearer {identity(sub='release-reviewer')}"}
    assert client.post(f"/api/requests/{rid}/decisions", headers=reviewer, json=decision).status_code == 202


def test_production_mode_fails_closed():
    with pytest.raises(ValueError, match="HTTPS agent gateway"):
        Settings(mode="temporal", _env_file=None)
    with pytest.raises(ValueError, match="OIDC authentication"):
        Settings(mode="temporal", gateway_url="https://gateway.example/execute", _env_file=None)
