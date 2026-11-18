"""Upload an Anthropic key through masked input; never put the key in arguments or files."""

import getpass
import shutil
import subprocess
import sys


def main():
    if not sys.stdin.isatty():
        raise SystemExit("Run this command in an interactive terminal for masked key entry.")
    cli = shutil.which("gcloud.cmd") or shutil.which("gcloud")
    if not cli:
        raise SystemExit("Google Cloud CLI is required.")
    key = getpass.getpass("Anthropic API key for sffactorygpt (hidden): ").strip()
    if not key:
        raise SystemExit("No key entered; nothing was uploaded.")
    result = subprocess.run(
        [
            cli,
            "secrets",
            "versions",
            "add",
            "factory-anthropic-key",
            "--project=sffactorygpt",
            "--data-file=-",
            "--format=value(name)",
            "--quiet",
        ],
        input=key,
        text=True,
        capture_output=True,
    )
    key = ""
    if result.returncode:
        raise SystemExit("Secret upload failed. Verify Google Cloud sign-in and Secret Manager permissions.")
    print("Key stored in Secret Manager for sffactorygpt. No local key file was created.")
    print("Secret version:", result.stdout.strip())


if __name__ == "__main__":
    main()
