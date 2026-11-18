import copy
import json
import signal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from factory.api import create_app
from factory.cloud import CloudConfig, manifests
from factory.dispatch import Dispatcher
from factory.lifecycle import shutdown_event
from factory.server import server_options
from factory.store import Outbox


def cloud_config():
    # Keep the shipped template executable as configuration evolves.
    return json.loads((Path(__file__).parents[1] / "deploy/gcp/config.example.json").read_text())


def test_cloud_manifests_keep_background_work_and_secrets_out_of_http_service():
    rendered = manifests(CloudConfig.model_validate(cloud_config()))
    containers = {}
    for role, manifest in rendered.items():
        pod = manifest["spec"]["template"]["spec"]
        if role == "migrate":
            assert pod["parallelism"] == pod["taskCount"] == 1
            pod = pod["template"]["spec"]
            assert pod["maxRetries"] == 0
        containers[role] = pod["containers"][0]
        env = {item["name"]: item for item in containers[role]["env"]}
        assert env["FACTORY_AUTH_MODE"]["value"] == "oidc"
        assert env["FACTORY_MODE"]["value"] == "gateway"
        assert env["FACTORY_RUNTIME"]["value"] == "postgres"
        assert env["FACTORY_DISPATCH_ENABLED"]["value"] == "false"
        assert "valueFrom" in env["FACTORY_DATABASE_URL"]
        assert not any("TEMPORAL" in name for name in env)
        assert ("FACTORY_GATEWAY_TOKEN" in env) == (role == "worker")
    assert len({item["image"] for item in containers.values()}) == 1
    assert set(rendered) == {"api", "worker", "migrate"}
    assert rendered["worker"]["kind"] == "WorkerPool"
    assert rendered["api"]["metadata"]["annotations"]["run.googleapis.com/ingress"] != "all"


@pytest.mark.parametrize("field,value", [
    ("image", "us-central1-docker.pkg.dev/your-project-id/factory/software-factory:latest"),
    ("worker_instances", 0),
    ("gateway_url", "http://gateway.example.com"),
    ("gateway_url", "https://user:password@gateway.example.com"),
    ("repositories", ["*"]),
    ("cloud_sql_instance", "another-project:us-central1:factory-db"),
])
def test_cloud_config_rejects_unsafe_release_inputs(field, value):
    data = cloud_config()
    data[field] = value
    with pytest.raises(ValidationError):
        CloudConfig.model_validate(data)


def test_cloud_config_requires_pinned_secrets_and_separate_service_accounts():
    data = cloud_config()
    bad_secret = copy.deepcopy(data)
    bad_secret["gateway_token"]["version"] = "latest"
    with pytest.raises(ValidationError):
        CloudConfig.model_validate(bad_secret)
    data["api"]["service_account"] = data["migrate"]["service_account"]
    with pytest.raises(ValidationError):
        CloudConfig.model_validate(data)


def test_port_and_cloud_proxy_boundary():
    assert server_options({})["port"] == 8000
    assert server_options({"PORT": "8080", "K_SERVICE": "factory-api"})["forwarded_allow_ips"] == "*"
    assert server_options({"PORT": "8080"})["forwarded_allow_ips"] == "127.0.0.1"
    for value in ("0", "65536", "invalid"):
        with pytest.raises(ValueError, match="PORT"):
            server_options({"PORT": value})


def test_disabled_http_dispatch_preserves_outbox_for_separate_process(config, work, monkeypatch):
    config.dispatch_enabled = False

    async def unexpected_dispatch(self):
        pytest.fail("HTTP service must not poll the outbox when dispatcher is separate")

    monkeypatch.setattr(Dispatcher, "run", unexpected_dispatch)
    app = create_app(config)
    with TestClient(app) as client:
        response = client.post("/api/requests", headers={"Idempotency-Key": "cloud-test"}, json=work.model_dump())
        assert response.status_code == 202
        assert client.get("/readyz").status_code == 200
        with app.state.store.session() as session:
            command = session.scalar(select(Outbox))
            assert command.request_id == response.json()["request_id"]
            assert not command.delivered


async def test_termination_wakes_shutdown_and_restores_host_handler():
    previous = signal.getsignal(signal.SIGTERM)
    with shutdown_event() as stopping:
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        import asyncio

        await asyncio.wait_for(stopping.wait(), timeout=1)
    assert signal.getsignal(signal.SIGTERM) == previous
