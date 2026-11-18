"""Git workspaces, confined file tools and the command sandbox.

Each request has one bare clone and one integration branch. Every agent call works in its own
worktree; a writing call commits there and is merged into the integration branch under a lock.
Nothing leaves this machine until a release is approved.
"""

import asyncio
import base64
import fnmatch
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from gateway.config import Repository, Settings

IDENTITY = ["-c", "user.name=Software Factory", "-c", "user.email=software-factory@users.noreply.github.com"]
LOCKS: dict[str, asyncio.Lock] = {}
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"),
    "Anthropic key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "hard-coded credential": re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"
    ),
}


class GitError(Exception):
    pass


class ToolError(Exception):
    """Shown to the model as a failed tool call."""


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", name)


def cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated by the gateway: {len(text) - limit} more characters not shown]"


class Workspace:
    def __init__(self, config: Settings, repo: Repository, request_id: str):
        self.config, self.repo, self.request_id = config, repo, request_id
        self.root = Path(config.workspace_root).resolve() / safe(request_id)
        self.git_dir = self.root / "repo.git"
        self.branch = f"factory/{safe(request_id)}"
        self.lock = LOCKS.setdefault(request_id, asyncio.Lock())

    # --- git plumbing -----------------------------------------------------------------------

    def _env(self, remote: bool) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GATEWAY_", "ANTHROPIC_", "GIT_"))}
        env["GIT_TERMINAL_PROMPT"] = "0"
        token = self.config.github_token.get_secret_value()
        if remote and token and self.repo.url.startswith("https://github.com/"):
            # Passed through the environment so the token is never written to disk or a command line.
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}",
            )
        return env

    def _git(self, *args, cwd=None, remote=False, check=True) -> str:
        # No credential helper: the gateway must never act with the credentials of whoever runs it.
        command = ["git", "-c", "credential.helper=", "-c", "core.autocrlf=false"]
        done = subprocess.run(
            [*command, *args],
            cwd=cwd or self.git_dir,
            env=self._env(remote),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        if check and done.returncode:
            verb = next((a for a in args if not a.startswith("-") and "=" not in a), "command")
            raise GitError(f"git {verb} failed: {(done.stderr or done.stdout).strip()[-400:]}")
        return done.stdout if check else f"{done.returncode}\n{done.stdout}"

    async def git(self, *args, **kwargs) -> str:
        return await asyncio.to_thread(self._git, *args, **kwargs)

    async def prepare(self):
        """Clone once per request and start the integration branch at the base commit."""
        async with self.lock:
            if not (self.git_dir / "HEAD").exists():
                self.root.mkdir(parents=True, exist_ok=True)
                shutil.rmtree(self.git_dir, ignore_errors=True)
                await self.git(
                    "clone", "--bare", "--single-branch", "--branch", self.repo.base, self.repo.url,
                    str(self.git_dir), cwd=self.root, remote=True,
                )
            exists = await self.git("rev-parse", "--verify", "--quiet", f"refs/heads/{self.branch}", check=False)
            if exists.startswith("1"):
                await self.git("branch", self.branch, self.repo.base)

    async def tip(self) -> str:
        return (await self.git("rev-parse", f"refs/heads/{self.branch}")).strip()

    async def base(self) -> str:
        return (await self.git("rev-parse", f"refs/heads/{self.repo.base}")).strip()

    async def checkout(self, key: str, writable: bool) -> Path:
        """A fresh worktree for one call. Anything left by an earlier attempt of this call is discarded."""
        path = self.root / "wt" / safe(key)
        async with self.lock:
            await self.discard(key)
            if writable:
                await self.git("worktree", "add", "--force", "-B", f"work/{safe(key)}", str(path), self.branch)
            else:
                await self.git("worktree", "add", "--force", "--detach", str(path), self.branch)
        return path

    async def discard(self, key: str):
        path = self.root / "wt" / safe(key)
        await self.git("worktree", "remove", "--force", str(path), check=False)
        await asyncio.to_thread(shutil.rmtree, path, True)
        await self.git("worktree", "prune", check=False)

    async def release(self, key: str):
        async with self.lock:
            await self.discard(key)

    async def find_commit(self, key: str) -> str | None:
        """The commit an earlier attempt of this call already integrated, if there is one."""
        log = await self.git("log", "--format=%H%x00%B%x01", f"{self.repo.base}..{self.branch}")
        for entry in log.split("\x01"):
            sha, _, body = entry.strip().partition("\x00")
            if sha and f"Factory-Key: {key}" in body.splitlines():
                return sha
        return None

    async def commit(self, path: Path, key: str, title: str) -> tuple[str, str] | None:
        """Commit the worktree and merge it into the integration branch. Returns (commit, patch)."""
        await self.git("add", "-A", cwd=path)
        staged = await self.git("diff", "--cached", "--quiet", cwd=path, check=False)
        if staged.startswith("0"):
            return None
        message = f"{title[:72]}\n\nFactory-Request: {self.request_id}\nFactory-Key: {key}"
        await self.git(*IDENTITY, "commit", "--quiet", "-m", message, cwd=path)
        commit = (await self.git("rev-parse", "HEAD", cwd=path)).strip()
        patch = await self.git("show", "--format=", "--no-color", commit, cwd=path)
        async with self.lock:
            before = await self.tip()
            merged = await self.git(
                *IDENTITY, "merge", "--no-edit", "-m", f"Integrate {key}\n\nFactory-Request: {self.request_id}",
                before, cwd=path, check=False,
            )
            if not merged.startswith("0"):
                await self.git("merge", "--abort", cwd=path, check=False)
                raise Conflict(merged[-600:])
            head = (await self.git("rev-parse", "HEAD", cwd=path)).strip()
            await self.git("update-ref", f"refs/heads/{self.branch}", head, before)
        return commit, patch

    async def diff(self, since: str | None = None) -> str:
        return await self.git("diff", "--no-color", f"{since or await self.base()}..{self.branch}")

    async def changed_files(self, since: str) -> list[str]:
        ancestor = await self.git("merge-base", "--is-ancestor", since, self.branch, check=False)
        if not ancestor.startswith("0"):
            raise GitError("The tested commit is not part of the integration branch")
        names = await self.git("diff", "--name-only", f"{since}..{self.branch}")
        return [line for line in names.splitlines() if line]

    async def push(self) -> str:
        remote = f"factory/{safe(self.request_id)[:8]}"
        await self.git("push", "--force", self.repo.url, f"refs/heads/{self.branch}:refs/heads/{remote}", remote=True)
        return remote


class Conflict(Exception):
    pass


# --- file tools ---------------------------------------------------------------------------------

READ_TOOLS = [
    {
        "name": "list_files",
        "description": "List files in the repository, optionally under a directory. Returns up to 500 paths.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory relative to the repository root"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file with line numbers. Use offset and limit for large files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "description": "First line to return, starting at 1"},
                "limit": {"type": "integer", "description": "Number of lines, at most 800"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search",
        "description": "Search file contents with an extended regular expression. Returns up to 100 matching lines.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Limit the search to this file or directory"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
]
WRITE_TOOLS = [
    {
        "name": "write_file",
        "description": "Create a file or replace its whole content.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_file",
        "description": "Replace one exact occurrence of old_text in a file with new_text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_file",
        "description": "Delete a file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
]


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path.lower(), pattern.lower()) for pattern in patterns)


class Files:
    """File access for one agent call, confined to one worktree."""

    def __init__(self, root: Path, repo: Repository, mode: str):
        # mode: "read", "write" (anything but protected paths) or "docs" (documentation paths only)
        self.root, self.repo, self.mode = root.resolve(), repo, mode
        self.calls = 0

    @property
    def tools(self) -> list[dict]:
        return READ_TOOLS + (WRITE_TOOLS if self.mode != "read" else [])

    def resolve(self, raw, write=False) -> Path:
        if not isinstance(raw, str) or "\x00" in raw:
            raise ToolError("path must be a string")
        target = (self.root / raw).resolve()
        try:
            relative = target.relative_to(self.root).as_posix()
        except ValueError:
            raise ToolError("That path is outside the repository") from None
        if relative.lower().split("/")[0] == ".git":
            raise ToolError("The .git directory is not accessible")
        if write:
            if self.mode == "read" or relative in ("", "."):
                raise ToolError("This agent cannot write there")
            if matches(relative, self.repo.protected_paths):
                raise ToolError("That path is protected and cannot be changed by an agent")
            if self.mode == "docs" and not matches(relative, self.repo.documentation_paths):
                raise ToolError(
                    "The documentation agent may only write: " + ", ".join(self.repo.documentation_paths)
                )
        return target

    def run(self, name: str, args: dict) -> str:
        self.calls += 1
        handler = getattr(self, f"tool_{name}", None)
        if not handler or name not in {tool["name"] for tool in self.tools}:
            raise ToolError(f"Unknown tool: {name}")
        if not isinstance(args, dict):
            raise ToolError("Tool input must be an object")
        try:
            return handler(**args)
        except TypeError:
            raise ToolError("Invalid arguments for " + name) from None
        except (OSError, UnicodeError) as exc:
            raise ToolError(type(exc).__name__ + ": " + str(exc)[:200]) from None

    def tool_list_files(self, path: str = "") -> str:
        start, found = self.resolve(path), []
        if not start.is_dir():
            raise ToolError("Not a directory")
        for folder, folders, names in os.walk(start):
            folders[:] = sorted(d for d in folders if d != ".git")
            for name in sorted(names):
                found.append((Path(folder) / name).relative_to(self.root).as_posix())
                if len(found) > 500:
                    return "\n".join(found[:500]) + "\n[more files exist; list a subdirectory]"
        return "\n".join(found) or "(no files)"

    def tool_read_file(self, path: str, offset: int = 1, limit: int = 400) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError("No such file")
        if target.stat().st_size > 2_000_000:
            raise ToolError("File is larger than 2 MB")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        first = max(int(offset), 1)
        chosen = lines[first - 1 : first - 1 + min(max(int(limit), 1), 800)]
        text = "\n".join(f"{first + i}\t{line[:2000]}" for i, line in enumerate(chosen))
        rest = len(lines) - (first - 1 + len(chosen))
        return text + (f"\n[{rest} more lines; continue with offset={first + len(chosen)}]" if rest > 0 else "")

    def tool_search(self, pattern: str, path: str = "") -> str:
        scope = self.resolve(path).relative_to(self.root).as_posix()
        done = subprocess.run(
            ["git", "grep", "-nIE", "--untracked", "-e", pattern, "--", scope],
            cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if done.returncode > 1:
            raise ToolError("Invalid search: " + done.stderr.strip()[:200])
        lines = done.stdout.splitlines()
        extra = f"\n[{len(lines) - 100} more matches not shown]" if len(lines) > 100 else ""
        return "\n".join(line[:400] for line in lines[:100]) + extra or "(no matches)"

    def tool_write_file(self, path: str, content: str) -> str:
        target = self.resolve(path, write=True)
        if len(content) > 400_000:
            raise ToolError("Content is larger than 400 KB")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
        return f"Wrote {path}"

    def tool_edit_file(self, path: str, old_text: str, new_text: str) -> str:
        target = self.resolve(path, write=True)
        if not target.is_file():
            raise ToolError("No such file")
        text = target.read_bytes().decode("utf-8")
        count = text.count(old_text) if old_text else 0
        if count != 1:
            raise ToolError(f"old_text must match exactly once; it matched {count} times")
        target.write_bytes(text.replace(old_text, new_text).encode("utf-8"))
        return f"Edited {path}"

    def tool_delete_file(self, path: str) -> str:
        target = self.resolve(path, write=True)
        if not target.is_file():
            raise ToolError("No such file")
        target.unlink()
        return f"Deleted {path}"


# --- sandbox ------------------------------------------------------------------------------------


def scan_secrets(patch: str) -> list[str]:
    """Credential-shaped text on added lines of a patch."""
    findings, current = [], "?"
    for line in patch.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+"):
            for label, pattern in SECRET_PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"Possible {label} added in {current}")
    return list(dict.fromkeys(findings))[:30]


def _run(command: list[str] | str, cwd: Path, env: dict, timeout: int) -> tuple[int, str, float]:
    started = time.monotonic()
    posix = os.name != "nt"
    process = subprocess.Popen(
        command, cwd=cwd, env=env, shell=isinstance(command, str), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=posix,
        creationflags=0 if posix else subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
        code = process.returncode
    except subprocess.TimeoutExpired:
        if posix:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
        output, _ = process.communicate()
        code, output = 124, output + f"\n[stopped after {timeout} seconds]".encode()
    return code, output.decode("utf-8", errors="replace"), round(time.monotonic() - started, 2)


async def run_command(config: Settings, repo: Repository, cwd: Path, command: str) -> tuple[int, str, float]:
    """Run an administrator-configured command against a worktree. The model never chooses the command."""
    if config.sandbox == "docker":
        argv = [
            "docker", "run", "--rm", "--network", "none", "--memory", "2g", "--cpus", "2", "--pids-limit", "512",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-v", f"{cwd}:/work", "-w", "/work", repo.image, "sh", "-c", command,
        ]
        return await asyncio.to_thread(_run, argv, cwd, dict(os.environ), config.command_timeout_seconds)
    keep = {"PATH", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG", "HOME",
            "USERPROFILE", "WINDIR"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    return await asyncio.to_thread(_run, command, cwd, env, config.command_timeout_seconds)
