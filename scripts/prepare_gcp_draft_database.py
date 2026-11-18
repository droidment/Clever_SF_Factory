"""Provision dedicated database credentials through APIs without printing secrets."""

import base64
import secrets
import shutil
import subprocess

import httpx
from sqlalchemy.engine import URL, make_url

PROJECT = "sffactorygpt"
INSTANCE = "factory-drafts-db"
SOCKET = f"/cloudsql/{PROJECT}:us-central1:{INSTANCE}"


def main():
    cli = shutil.which("gcloud.cmd") or shutil.which("gcloud")
    token = subprocess.check_output([cli, "auth", "print-access-token", "--quiet"], text=True).strip()
    with httpx.Client(headers={"Authorization": "Bearer " + token}, timeout=60) as client:
        def checked(method, url, **kwargs):
            response = client.request(method, url, **kwargs)
            if response.status_code >= 400:
                raise SystemExit(f"Cloud provisioning failed: HTTP {response.status_code}; secret values suppressed")
            return response.json()

        def connection_secret(name, username):
            root = f"https://secretmanager.googleapis.com/v1/projects/{PROJECT}/secrets"
            result = client.get(f"{root}/{name}/versions/latest:access")
            if result.status_code == 200:
                return base64.b64decode(result.json()["payload"]["data"]).decode()
            if result.status_code != 404:
                raise SystemExit(f"Cannot access dedicated database secret: HTTP {result.status_code}")
            present = client.get(f"{root}/{name}")
            if present.status_code == 404:
                checked("POST", root, params={"secretId": name}, json={"replication": {"automatic": {}}})
            elif present.status_code != 200:
                raise SystemExit(f"Cannot inspect dedicated database secret: HTTP {present.status_code}")
            url = URL.create("postgresql+psycopg", username=username, password=secrets.token_urlsafe(36),
                             database="factory", query={"host": SOCKET, "connect_timeout": "10"})
            value = url.render_as_string(hide_password=False)
            checked("POST", f"{root}/{name}:addVersion", json={"payload": {
                "data": base64.b64encode(value.encode()).decode(),
            }})
            return value

        owner_url = connection_secret("factory-drafts-owner-url", "factory_owner")
        connection_secret("factory-drafts-database-url", "factory_app")
        root = f"https://sqladmin.googleapis.com/sql/v1beta4/projects/{PROJECT}/instances/{INSTANCE}"
        users = checked("GET", root + "/users").get("items", [])
        if not any(user["name"] == "factory_owner" for user in users):
            operation = checked("POST", root + "/users", json={
                "name": "factory_owner", "password": make_url(owner_url).password, "type": "BUILT_IN",
            })
            print("Database owner creation operation:", operation["name"])
        print("Dedicated database secrets are stored in Secret Manager. Values were not displayed or written locally.")


if __name__ == "__main__":
    main()
