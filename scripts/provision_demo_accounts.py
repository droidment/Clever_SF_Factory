"""Create hashed demo credentials in Secret Manager; show the shared password once."""

import base64
import json
import secrets
import shutil
import subprocess

import httpx

from factory.demo_accounts import make_credentials


def main():
    cli = shutil.which('gcloud.cmd') or shutil.which('gcloud')
    token = subprocess.check_output([cli, 'auth', 'print-access-token'], text=True).strip()
    root = 'https://secretmanager.googleapis.com/v1/projects/sffactorygpt/secrets'
    name = 'factory-demo-credentials'
    password = 'Demo-' + secrets.token_hex(6) + '!'
    with httpx.Client(headers={'Authorization': 'Bearer ' + token}, timeout=45) as client:
        existing = client.get(root + '/' + name)
        if existing.status_code != 404:
            raise SystemExit('Secret already exists or cannot be checked; refusing to replace credentials.')
        response = client.post(root, params={'secretId': name}, json={'replication': {'automatic': {}}})
        if response.status_code >= 400:
            raise SystemExit('Could not create demo credential secret')
        value = json.dumps(make_credentials(password))
        response = client.post(root + '/' + name + ':addVersion', json={'payload': {'data': base64.b64encode(value.encode()).decode()}})
        if response.status_code >= 400:
            raise SystemExit('Could not store demo credential secret')
        print('Secret version:', response.json()['name'])
        print('Shared demo password (provide privately to the presenter):', password)


if __name__ == '__main__':
    main()
