"""The execution gateway, against a real git repository and a real test run.

Claude is replaced by a scripted HTTP transport, so these tests spend nothing. Everything else is
real: clones, worktrees, commits, merges, the sandboxed test command and the push to the origin.
"""

import asyncio
import json
import re
import subprocess
import sys
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from anthropic import AsyncAnthropic
from pydantic import SecretStr
from sqlalchemy import select

from factory.agents import AgentService
from factory.contracts import Plan, TaskSpec, Triage
from factory.durable import DurableWorker
from factory.store import Decision, Outbox, Store, WorkRequest
from gateway.app import create_app
from gateway.config import Repository, Settings
from gateway.store import Operation
from gateway.workspace import Files, ToolError, scan_secrets

TOKEN = "gateway-test-token-not-a-real-secret-0001"
FAKE_KEY = "test-only-not-a-real-anthropic-key"
GATE = "r0:policy-approval"


def git(*args, cwd):
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "core.autocrlf=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture
def origin(tmp_path):
    """A bare repository with a bug and a test that catches it."""
    source = tmp_path / "source"
    source.mkdir()
    git("init", "--quiet", "-b", "main", cwd=source)
    (source / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8", newline="")
    (source / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8", newline=""
    )
    git("add", "-A", cwd=source)
    git("commit", "--quiet", "-m", "Initial", cwd=source)
    bare = tmp_path / "origin.git"
    git("clone", "--quiet", "--bare", str(source), str(bare), cwd=tmp_path)
    return bare


@pytest.fixture
def gateway_config(tmp_path, origin):
    repositories = tmp_path / "repositories.json"
    repositories.write_text(
        json.dumps(
            {
                "platform/order-service": {
                    "url": str(origin),
                    "test_command": f'"{sys.executable}" -m pytest -q -p no:cacheprovider',
                }
            }
        ),
        encoding="utf-8",
    )
    return Settings(
        token=TOKEN,
        anthropic_api_key=FAKE_KEY,
        database_url=f"sqlite:///{tmp_path / 'gateway.db'}",
        workspace_root=str(tmp_path / "workspaces"),
        repositories_file=str(repositories),
        sandbox="process",
        _env_file=None,
    )


@pytest.fixture
def claude(monkeypatch):
    """Scripted model: each agent replays a fixed list of tool calls, or returns a fixed structure."""
    state = SimpleNamespace(
        calls=[],
        scripts={
            "developer": [
                ("read_file", {"path": "calc.py"}),
                ("edit_file", {"path": "calc.py", "old_text": "a - b", "new_text": "a + b"}),
                ("submit", {"summary": "add() subtracted; it now adds."}),
            ],
            "documentation": [
                ("write_file", {"path": "calc.py", "content": "broken"}),  # refused: not a documentation path
                ("write_file", {"path": "docs/CHANGES.md", "content": "# Changes\n\nadd() now adds.\n"}),
                ("submit", {"summary": "Documented the fix."}),
            ],
            "review": [
                ("read_file", {"path": "calc.py"}),
                ("submit", {"summary": "The fix is correct.", "document": "# Review\n\nNo defects found."}),
            ],
        },
        structured={
            "triage": lambda context: Triage(
                category="bug_fix", complexity="small", summary="A one-line arithmetic bug.", agents=["developer"]
            ).model_dump(),
            "planner": lambda context: Plan(
                rationale="One fix.", tasks=[TaskSpec(id="fix", agent="developer", title="Fix add()")]
            ).model_dump(),
            "validation": lambda context: {
                "summary": "Every criterion is covered by the passing test run.",
                "criteria": [
                    {"criterion": c, "passed": True, "evidence": ["policy-test/test-report.json", "made-up/evidence"]}
                    for c in context["acceptance_criteria"]
                ],
            },
        },
    )

    def handler(request):
        assert request.url.host == "api.anthropic.com"
        assert request.headers["x-api-key"] == FAKE_KEY
        body = json.loads(request.content)
        state.calls.append(body)
        agent = re.match(r"You are the (\w+) specialist", body["system"]).group(1)
        turn = sum(message["role"] == "assistant" for message in body["messages"])
        if "format" in body["output_config"]:
            assert "tools" not in body
            output = state.structured[agent](json.loads(body["messages"][0]["content"]))
            content, stop = [{"type": "text", "text": json.dumps(output)}], "end_turn"
        else:
            tool, arguments = state.scripts[agent][turn]
            assert tool in {t["name"] for t in body["tools"]}
            content, stop = [{"type": "tool_use", "id": f"toolu_{turn}", "name": tool, "input": arguments}], "tool_use"
        return httpx2.Response(
            200,
            json={
                "id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
                "content": content, "stop_reason": stop, "stop_sequence": None,
                "usage": {"input_tokens": 1000, "output_tokens": 200},
            },
        )

    def client(**kwargs):
        assert kwargs["base_url"] == "https://api.anthropic.com"
        return AsyncAnthropic(**kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))

    monkeypatch.setattr("gateway.model.AsyncAnthropic", client)
    return state


def request_body(work, agent, key, request_id="req-1", **extra):
    return {
        "schema_version": 1, "request_id": request_id, "tenant_id": "demo", "agent": agent, "key": key,
        "work": work.model_dump(mode="json"), "dependencies": {}, "repair_context": [], "max_cost_usd": 2, **extra,
    }


async def post(app, body, token=TOKEN):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as client:
        return await client.post(
            "/execute",
            json=body,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"{body['request_id']}:{body['key']}"},
        )


async def finished(app, body):
    for _ in range(600):
        response = await post(app, body)
        if response.status_code != 202:
            return response
        await asyncio.sleep(0.05)
    pytest.fail("Gateway operation did not finish")


async def test_callers_without_the_secret_or_a_configured_repository_are_refused(gateway_config, work, claude):
    app = create_app(gateway_config)
    assert (await post(app, request_body(work, "developer", "r0:fix"), token="wrong")).status_code == 401
    work.repository = "platform/customer-portal"
    assert (await post(app, request_body(work, "developer", "r0:fix"))).status_code == 403
    assert not claude.calls


async def test_change_is_committed_and_a_repeat_returns_the_same_receipt(gateway_config, work, claude):
    app = create_app(gateway_config)
    body = request_body(work, "developer", "r0:fix")
    first = await post(app, body)
    assert first.status_code == 200, first.text
    result = first.json()
    assert "+    return a + b" in result["artifacts"][0]["content"]
    assert result["passed"] and result["tool_calls"][0]["commit"]
    # Three model turns at 1,000 input and 200 output tokens each, at the configured model's rates.
    assert result["usage"] == {"cost_usd": 0.024, "tokens": 3600, "model": "claude-opus-5-5"}
    turns = len(claude.calls)

    again = await post(app, body)
    assert again.json() == result and len(claude.calls) == turns
    changed = await post(app, {**body, "repair_context": ["different request, same key"]})
    assert changed.status_code == 422 and len(claude.calls) == turns


async def test_long_call_answers_202_until_the_result_is_ready(gateway_config, work, claude):
    app = create_app(gateway_config.model_copy(update={"wait_seconds": 0}))
    body = request_body(work, "developer", "r0:fix")
    assert (await post(app, body)).status_code == 202
    assert (await finished(app, body)).json()["passed"]
    assert sum(len(call["messages"]) == 1 for call in claude.calls) == 1  # the model was started once


async def test_failing_tests_are_reported_with_their_output(gateway_config, work, claude):
    app = create_app(gateway_config)
    result = (await post(app, request_body(work, "test", "r0:policy-test"))).json()
    assert result["passed"] is False
    assert "exit code 1" in result["findings"][0] and "assert" in result["findings"][0]
    report = json.loads(result["artifacts"][0]["content"])
    assert report["exit_code"] == 1 and report["commit"] == result["tool_calls"][0]["commit"]
    assert not claude.calls  # the test step never asks a model whether tests passed


async def test_committed_change_is_recovered_when_the_receipt_was_lost(gateway_config, work, claude):
    app = create_app(gateway_config)
    body = request_body(work, "developer", "r0:fix")
    first = (await post(app, body)).json()
    turns = len(claude.calls)
    # The gateway stopped after merging the commit but before recording the result.
    with app.state.store.session.begin() as s:
        op = s.scalar(select(Operation))
        op.status, op.result = "failed", None
    second = (await post(app, body)).json()
    assert second["tool_calls"][0]["commit"] == first["tool_calls"][0]["commit"]
    assert second["tool_calls"][0]["recovered"] and len(claude.calls) == turns
    assert second["artifacts"][0]["content"] == first["artifacts"][0]["content"]


async def test_call_stops_before_exceeding_its_authorised_cost(gateway_config, work, claude):
    app = create_app(gateway_config)
    response = await post(app, request_body(work, "developer", "r0:fix", max_cost_usd=0.025))
    assert response.status_code == 422 and "limit" in response.json()["error"]
    assert len(claude.calls) == 1
    assert app.state.store.spent("req-1") == pytest.approx(0.008)  # the turn that did run is still counted


async def test_validation_needs_the_gateways_own_passing_test_run(gateway_config, work, claude):
    app = create_app(gateway_config)
    result = (await post(app, request_body(work, "validation", "r0:policy-validation"))).json()
    assert result["passed"] is False and not any(c["passed"] for c in result["criteria"])
    assert "no passing test run" in result["findings"][0] and not claude.calls


def test_file_tools_stay_inside_the_checkout_and_respect_path_rules(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "calc.py").write_text("x = 1\n", encoding="utf-8")
    writer, author, reader = (Files(tmp_path, Repository(url="unused"), mode) for mode in ("write", "docs", "read"))
    for path in ("../outside.txt", ".git/config", ".GIT/hooks/pre-commit", ".github/workflows/ci.yml"):
        with pytest.raises(ToolError):
            writer.run("write_file", {"path": path, "content": "x"})
    with pytest.raises(ToolError):
        writer.run("read_file", {"path": "../../etc/passwd"})
    with pytest.raises(ToolError, match="documentation agent"):
        author.run("write_file", {"path": "calc.py", "content": "x"})
    with pytest.raises(ToolError, match="Unknown tool"):
        reader.run("write_file", {"path": "notes.md", "content": "x"})
    assert author.run("write_file", {"path": "docs/guide.md", "content": "# Guide\n"}) == "Wrote docs/guide.md"
    assert writer.run("edit_file", {"path": "calc.py", "old_text": "1", "new_text": "2"}) == "Edited calc.py"
    assert reader.run("read_file", {"path": "calc.py"}) == "1\tx = 2"
    assert not (tmp_path.parent / "outside.txt").exists()


def test_secret_scan_reads_only_added_lines():
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    patch = f"+++ b/settings.py\n+AWS = '{key}'\n-OLD = '{key}'\n+++ b/app.py\n+total = price + tax\n"
    assert scan_secrets(patch) == ["Possible AWS access key added in settings.py"]


async def drain(worker):
    for _ in range(80):
        await worker.tick()
        running = list(worker.tasks.values())
        if not running:
            return
        await asyncio.gather(*running, return_exceptions=True)
    pytest.fail("Worker did not become idle")


async def test_factory_delivers_a_tested_change_through_the_gateway(
    config, work, gateway_config, origin, claude, monkeypatch
):
    work.deploy = True
    gateway_config.repositories["platform/order-service"].github = "acme/order-service"
    hosted = []

    def github(request):
        assert request.headers["authorization"] == "Bearer test-only-github-token"
        hosted.append((request.method, request.url.path, json.loads(request.content or b"null")))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"html_url": "https://github.example/acme/order-service/pull/7"})

    monkeypatch.setattr("gateway.agents.github_transport", httpx.MockTransport(github))
    gateway = create_app(
        gateway_config.model_copy(update={"wait_seconds": 0.2, "github_token": SecretStr("test-only-github-token")})
    )
    monkeypatch.setattr(AgentService, "transport", httpx.ASGITransport(app=gateway))
    monkeypatch.setattr(AgentService, "poll_scale", 0.02)
    factory = config.model_copy(
        update={"mode": "gateway", "gateway_url": "http://gateway.test/execute", "gateway_token": TOKEN}
    )
    store = Store(factory)
    store.create_schema()
    request_id, _ = store.intake("demo", "operator", "gateway-e2e", work, "gateway")
    worker = DurableWorker(store, factory, worker_id="a", retry_base=0.01)

    def status():
        with store.session() as s:
            return s.get(WorkRequest, request_id).status

    await drain(worker)
    assert status() == "waiting_for_human"
    branch = f"factory/{request_id[:8]}"
    assert branch not in git("branch", "--list", cwd=origin)  # nothing leaves the gateway before approval

    decision = {"gate": GATE, "approved": True, "comment": "Reviewed the patch and the test report"}
    with store.session.begin() as s:
        s.add(Decision(request_id=request_id, gate=GATE, actor="reviewer", data=decision))
        s.add(Outbox(request_id=request_id, command_key=f"decision:{GATE}", type="decision", payload=decision))
    await drain(worker)
    assert status() == "completed"

    # The approved, tested change is on the origin; the documentation agent's refused write is not.
    assert git("show", f"{branch}:calc.py", cwd=origin) == "def add(a, b):\n    return a + b\n"
    assert "add() now adds" in git("show", f"{branch}:docs/CHANGES.md", cwd=origin)
    operations = {op.key: op for op in gateway.state.store.session().scalars(select(Operation))}
    assert all(op.status == "succeeded" and op.attempts == 1 for op in operations.values())
    assert json.loads(operations["r0:policy-test"].result["artifacts"][0]["content"])["exit_code"] == 0
    release = json.loads(operations["r0:policy-deploy"].result["artifacts"][0]["content"])
    assert release["pull_request"] == "https://github.example/acme/order-service/pull/7"
    assert [(method, path) for method, path, _ in hosted] == [
        ("GET", "/repos/acme/order-service/pulls"),
        ("POST", "/repos/acme/order-service/pulls"),
    ]
    assert hosted[-1][2]["head"] == branch and hosted[-1][2]["base"] == "main"
    for criterion in operations["r0:policy-validation"].result["criteria"]:
        assert criterion["passed"] and criterion["evidence"] == ["policy-test/test-report.json"]
    store.engine.dispose()
