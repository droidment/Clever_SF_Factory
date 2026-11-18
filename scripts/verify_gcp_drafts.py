"""Private cloud smoke checks. --submit explicitly makes a paid draft request."""

import argparse
import json
import shutil
import subprocess

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--submit', action='store_true')
    parser.add_argument('--request')
    parser.add_argument('--public-view', action='store_true')
    args = parser.parse_args()
    cli = shutil.which('gcloud.cmd') or shutil.which('gcloud')

    def cloud(*arguments):
        return subprocess.check_output([cli, *arguments], text=True).strip()

    url = cloud('run', 'services', 'describe', 'factory-drafts', '--project=sffactorygpt',
                '--region=us-central1', '--format=value(status.url)')
    token = cloud('auth', 'print-identity-token')
    with httpx.Client(base_url=url, timeout=45) as client:
        unsigned = client.get('/api/config')
        print('Unauthenticated access:', unsigned.status_code)
        if args.public_view:
            assert unsigned.status_code == 200 and unsigned.json()['roles'] == ['viewer']
            assert client.get('/').status_code == 200
            denied = client.post('/api/requests', headers={'Idempotency-Key': 'public-access-denied'}, json={
                'title': 'Public write must be denied', 'description': 'This read-only access check must not create or run a workflow.',
                'requestor': 'Public visitor', 'repository': 'platform/customer-portal',
            })
            assert denied.status_code == 403
            print('Anonymous workflow creation: blocked (403)')
        else:
            assert unsigned.status_code in (401, 403)
        client.headers['Authorization'] = 'Bearer ' + token
        client.headers['X-Serverless-Authorization'] = 'Bearer ' + token
        client.headers['X-Factory-Authorization'] = 'Bearer ' + token
        for path in ('/readyz', '/api/config'):
            response = client.get(path)
            print(path, response.status_code, response.text[:1200])
            response.raise_for_status()
        rejected = client.get('/api/config', headers={'X-Factory-Authorization': 'Bearer invalid'})
        print('Invalid application identity:', rejected.status_code)
        assert rejected.status_code == 401
        if args.submit:
            response = client.post('/api/requests', headers={
                'Idempotency-Key': 'gcp-deployment-smoke-20260924-v1',
            }, json={
                'title': 'Cloud deployment smoke test: operator notes',
                'description': 'Draft a short operator note for this supplied configuration: the Anthropic draft workspace runs on private Cloud Run in sffactorygpt/us-central1. Records persist in Cloud SQL PostgreSQL. The authorized user connects with the gcloud authenticated proxy. The Anthropic key is injected from Secret Manager. Produce documentation only, based on these supplied facts. Do not request repository access, make changes, execute tests, deploy software, or claim independent verification. No clarification is needed for this documentation-only request.',
                'requestor': 'Deployment verification', 'source': 'api',
                'repository': 'platform/customer-portal', 'priority': 'low',
                'acceptance_criteria': ['Proposed operator notes explain private access and persistent records, and clearly state that execution is unverified.'],
                'deploy': False, 'budget_usd': 20,
            })
            print('Submission:', response.status_code, response.text[:1200])
            response.raise_for_status()
        if args.request:
            response = client.get('/api/requests/' + args.request)
            response.raise_for_status()
            data = response.json()
            summary = {key: data[key] for key in ('id', 'status', 'stage', 'cost', 'tokens', 'gate')}
            summary['artifacts'] = len(data['artifacts'])
            summary['executions'] = [{key: item[key] for key in ('agent', 'status', 'error')}
                                     for item in data['executions']]
            print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
