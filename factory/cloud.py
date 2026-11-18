"""Render Cloud Run manifests locally. This module never calls a cloud API."""

import argparse
import json
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")]


class SecretRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,255}$")]
    version: Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]

    def env(self, name):
        return {"name": name, "valueFrom": {"secretKeyRef": {"name": self.name, "key": self.version}}}


class Role(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_account: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com$")]
    database: SecretRef


class CloudConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")]
    project_number: Annotated[str, Field(pattern=r"^[0-9]{6,20}$")]
    region: Annotated[str, Field(pattern=r"^[a-z]+-[a-z]+[0-9]+$")]
    name: Name = "software-factory"
    image: Annotated[str, Field(pattern=r"^[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9/_-]+@sha256:[a-f0-9]{64}$")]
    cloud_sql_instance: Annotated[str, Field(pattern=r"^[a-z0-9-]+:[a-z0-9-]+:[a-z0-9-]+$")]
    temporal_address: Annotated[str, Field(pattern=r"^[a-zA-Z0-9.-]+:7233$")]
    temporal_namespace: Annotated[str, Field(min_length=1)]
    task_queue: Annotated[str, Field(min_length=1)] = "factory-v1"
    oidc_issuer: str
    oidc_audience: Annotated[str, Field(min_length=1)]
    oidc_jwks_url: str
    gateway_url: str
    repositories: Annotated[list[str], Field(min_length=1)]
    api: Role
    worker: Role
    dispatcher: Role
    migrate: Role
    temporal_key: SecretRef
    gateway_token: SecretRef
    worker_instances: Annotated[int, Field(ge=1, le=20)] = 1
    api_max_instances: Annotated[int, Field(ge=1, le=20)] = 3

    @field_validator("oidc_issuer", "oidc_jwks_url", "gateway_url")
    @classmethod
    def https_endpoint(cls, value):
        from urllib.parse import urlsplit

        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("An HTTPS endpoint without embedded credentials is required")
        return value

    @field_validator("repositories")
    @classmethod
    def exact_repositories(cls, value):
        import re

        if len(set(value)) != len(value) or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) for repo in value
        ):
            raise ValueError("Use unique, exact owner/repository identifiers")
        return value

    @model_validator(mode="after")
    def resource_boundaries(self):
        if not self.image.startswith(f"{self.region}-docker.pkg.dev/{self.project_id}/"):
            raise ValueError("Image must belong to this project and region")
        if not self.cloud_sql_instance.startswith(f"{self.project_id}:{self.region}:"):
            raise ValueError("Cloud SQL must belong to this project and region")
        accounts = [getattr(self, role).service_account for role in ("api", "worker", "dispatcher", "migrate")]
        if len(set(accounts)) != 4 or any(
            not account.endswith(f"@{self.project_id}.iam.gserviceaccount.com") for account in accounts
        ):
            raise ValueError("Use four distinct service accounts in the selected project")
        return self


def manifests(config: CloudConfig) -> dict[str, dict]:
    common = {
        "FACTORY_MODE": "temporal",
        "FACTORY_AUTH_MODE": "oidc",
        "FACTORY_DISPATCH_ENABLED": "false",
        "FACTORY_TEMPORAL_ADDRESS": config.temporal_address,
        "FACTORY_TEMPORAL_NAMESPACE": config.temporal_namespace,
        "FACTORY_TEMPORAL_TLS": "true",
        "FACTORY_TASK_QUEUE": config.task_queue,
        "FACTORY_OIDC_ISSUER": config.oidc_issuer,
        "FACTORY_OIDC_AUDIENCE": config.oidc_audience,
        "FACTORY_OIDC_JWKS_URL": config.oidc_jwks_url,
        "FACTORY_GATEWAY_URL": config.gateway_url,
        "FACTORY_ALLOWED_REPOSITORIES": ",".join(config.repositories),
        "PYTHONUNBUFFERED": "1",
    }
    result = {}
    for role, module in {"api": "server", "worker": "worker", "dispatcher": "dispatch", "migrate": "migrate"}.items():
        identity = getattr(config, role)
        env = [{"name": key, "value": value} for key, value in common.items()]
        env.append(identity.database.env("FACTORY_DATABASE_URL"))
        if role in {"worker", "dispatcher"}:
            env.append(config.temporal_key.env("FACTORY_TEMPORAL_API_KEY"))
        if role == "worker":
            env.append(config.gateway_token.env("FACTORY_GATEWAY_TOKEN"))
        container = {
            "image": config.image,
            "command": ["python"],
            "args": ["-m", f"factory.{module}"],
            "env": env,
            "resources": {"limits": {"cpu": "1", "memory": "1Gi"}},
        }
        pod = {"serviceAccountName": identity.service_account, "containers": [container]}
        annotations = {"run.googleapis.com/cloudsql-instances": config.cloud_sql_instance}
        metadata = {
            "name": f"{config.name}-{role}",
            "namespace": config.project_number,
            "labels": {"cloud.googleapis.com/location": config.region},
        }
        if role == "api":
            metadata["annotations"] = {"run.googleapis.com/ingress": "internal-and-cloud-load-balancing"}
            annotations.update({
                "autoscaling.knative.dev/minScale": "0",
                "autoscaling.knative.dev/maxScale": str(config.api_max_instances),
                "run.googleapis.com/execution-environment": "gen2",
            })
            container["ports"] = [{"name": "http1", "containerPort": 8080}]
            container["startupProbe"] = {
                "httpGet": {"path": "/readyz", "port": 8080},
                "periodSeconds": 10, "timeoutSeconds": 5, "failureThreshold": 12,
            }
            container["livenessProbe"] = {
                "httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 30,
            }
            pod.update({"containerConcurrency": 40, "timeoutSeconds": 300})
            api_version, kind = "serving.knative.dev/v1", "Service"
            spec = {
                "template": {"metadata": {"annotations": annotations}, "spec": pod},
                "traffic": [{"latestRevision": True, "percent": 100}],
            }
        elif role == "migrate":
            api_version, kind = "run.googleapis.com/v1", "Job"
            pod.update({"maxRetries": 0, "timeoutSeconds": "300"})
            spec = {"template": {
                "metadata": {"annotations": annotations},
                "spec": {"parallelism": 1, "taskCount": 1, "template": {"spec": pod}},
            }}
        else:
            api_version, kind = "run.googleapis.com/v1", "WorkerPool"
            metadata["annotations"] = {
                "run.googleapis.com/manualInstanceCount": str(config.worker_instances if role == "worker" else 1),
            }
            spec = {
                "template": {"metadata": {"annotations": annotations}, "spec": pod},
                "instanceSplits": [{"latestRevision": True, "percent": 100}],
            }
        result[role] = {"apiVersion": api_version, "kind": kind, "metadata": metadata, "spec": spec}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--output", type=Path, default=Path("deploy/gcp/rendered"))
    args = parser.parse_args()
    config = CloudConfig.model_validate_json(args.config.read_text(encoding="utf-8-sig"))
    args.output.mkdir(parents=True, exist_ok=True)
    for name, manifest in manifests(config).items():
        # JSON is valid YAML 1.2, accepted by gcloud replace without a YAML dependency.
        (args.output / f"{name}.yaml").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Rendered four manifests in {args.output}. No cloud resources were changed.")


if __name__ == "__main__":
    main()
