"""Render the single-workspace Anthropic Cloud Run deployment; no cloud API calls."""

import json
from pathlib import Path


def main():
    config = json.loads(Path("deploy/gcp/draft.local.json").read_text())
    image = config["image"]
    if not image.startswith("us-central1-docker.pkg.dev/sffactorygpt/factory/software-factory@sha256:"):
        raise ValueError("An immutable image in the selected project's registry is required")
    annotation = {"run.googleapis.com/cloudsql-instances": "sffactorygpt:us-central1:factory-drafts-db"}

    def secret(variable, name, version="1"):
        return {"name": variable, "valueFrom": {"secretKeyRef": {"name": name, "key": version}}}

    def env(values):
        return [{"name": key, "value": str(value)} for key, value in values.items()]

    def metadata(name):
        return {"name": name, "namespace": config["project_number"],
                "labels": {"cloud.googleapis.com/location": config["region"]}}

    service_meta = metadata("factory-drafts")
    service_meta["annotations"] = {"run.googleapis.com/ingress": "all", "run.googleapis.com/invoker-iam-disabled": str(config.get("public_view", False)).lower()}
    service_env = env({
        "FACTORY_MODE": "anthropic", "FACTORY_AUTH_MODE": "google",
        "FACTORY_GOOGLE_AUDIENCE": config["google_audience"], "FACTORY_GOOGLE_ALLOWED_EMAILS": config["account"],
        "FACTORY_GOOGLE_TENANT": config["project"], "FACTORY_INITIALIZE_SCHEMA": "false",
        "FACTORY_PUBLIC_VIEW": str(config.get("public_view", False)).lower(),
        "FACTORY_DEMO_LOGIN_ENABLED": str(config.get("demo_login_enabled", False)).lower(),
        "FACTORY_DRAFT_REVIEW_REQUIRED": str(config.get("draft_review_required", False)).lower(),
        "FACTORY_DISPATCH_ENABLED": "true", "FACTORY_ANTHROPIC_MODEL": config.get("model", "claude-sonnet-5"),
        "FACTORY_ANTHROPIC_MAX_TOKENS": "4096", "FACTORY_TRUSTED_ORIGINS": "http://127.0.0.1:8085,http://localhost:8085",
        "FACTORY_ALLOWED_REPOSITORIES": "platform/customer-portal,platform/order-service,platform/shared-libraries",
        "PYTHONUNBUFFERED": "1",
    }) + [secret("FACTORY_DATABASE_URL", "factory-drafts-database-url"),
          secret("FACTORY_ANTHROPIC_API_KEY", "factory-anthropic-key", config.get("api_key_version", "1"))]
    if config.get("demo_login_enabled"):
        service_env.append(secret("FACTORY_DEMO_CREDENTIALS", "factory-demo-credentials", config["demo_credentials_version"]))
    service = {
        "apiVersion": "serving.knative.dev/v1", "kind": "Service", "metadata": service_meta,
        "spec": {
            "template": {"metadata": {"annotations": {**annotation,
                "run.googleapis.com/execution-environment": "gen2", "run.googleapis.com/cpu-throttling": "false",
                "autoscaling.knative.dev/minScale": "1", "autoscaling.knative.dev/maxScale": "1",
            }}, "spec": {
                "serviceAccountName": "factory-drafts@sffactorygpt.iam.gserviceaccount.com",
                "containerConcurrency": 20, "timeoutSeconds": 300,
                "containers": [{"image": image, "env": service_env, "ports": [{"containerPort": 8080}],
                    "resources": {"limits": {"cpu": "1", "memory": "1Gi"}},
                    "startupProbe": {"httpGet": {"path": "/readyz", "port": 8080}, "periodSeconds": 10, "timeoutSeconds": 5, "failureThreshold": 18},
                    "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 30},
                }],
            }}, "traffic": [{"latestRevision": True, "percent": 100}],
        },
    }
    job = {
        "apiVersion": "run.googleapis.com/v1", "kind": "Job", "metadata": metadata("factory-drafts-bootstrap"),
        "spec": {"template": {"metadata": {"annotations": annotation}, "spec": {
            "parallelism": 1, "taskCount": 1, "template": {"spec": {
                "serviceAccountName": "factory-drafts-migrate@sffactorygpt.iam.gserviceaccount.com",
                "maxRetries": 0, "timeoutSeconds": "300",
                "containers": [{"image": image, "command": ["python"], "args": ["-m", "factory.bootstrap_cloud"],
                    "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
                    "env": env({"FACTORY_MODE": "demo", "FACTORY_AUTH_MODE": "demo", "PYTHONUNBUFFERED": "1"}) + [
                        secret("FACTORY_DATABASE_URL", "factory-drafts-owner-url"),
                        secret("FACTORY_RUNTIME_DATABASE_URL", "factory-drafts-database-url"),
                    ],
                }],
            }},
        }}},
    }
    output = Path("deploy/gcp/rendered")
    output.mkdir(exist_ok=True)
    for name, manifest in {"draft-api": service, "draft-bootstrap": job}.items():
        (output / f"{name}.yaml").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Rendered draft service and database-bootstrap job")


if __name__ == "__main__":
    main()
