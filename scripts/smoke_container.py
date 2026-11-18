"""Verify the production image's non-root startup, PORT and SIGTERM contract."""

import argparse
import json
import subprocess
import uuid


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="software-factory:local")
    args = parser.parse_args()
    name = "factory-smoke-" + uuid.uuid4().hex[:12]
    docker("run", "-d", "--name", name, "-e", "PORT=8080", args.image)
    try:
        # No host ports or volumes; the demo database disappears with this container.
        check = """
import os, time, urllib.request
assert os.getuid() == 10001
for attempt in range(40):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=1) as response:
            assert response.status == 200
        break
    except OSError:
        time.sleep(0.25)
else:
    raise RuntimeError('Container did not become ready on PORT=8080')
with urllib.request.urlopen('http://127.0.0.1:8080/') as response:
    assert response.status == 200
    assert '<html' in response.read().decode().lower()
print('Non-root HTTP startup, database readiness and built console: passed')
"""
        print(docker("exec", name, "python", "-c", check))
        docker("stop", "--time", "12", name)
        state = json.loads(docker("inspect", "--format", "{{json .State}}", name))
        if state["ExitCode"] != 0 or state["OOMKilled"]:
            raise RuntimeError(f"Container shutdown failed: {state['ExitCode']}")
        print("SIGTERM shutdown: exit 0")
    finally:
        print(docker("logs", name))
        docker("rm", "-f", name)


if __name__ == "__main__":
    main()
