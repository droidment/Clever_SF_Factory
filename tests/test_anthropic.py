import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest
from anthropic import AsyncAnthropic
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from factory.agents import AgentService, PermanentAgentError
from factory.anthropic_provider import AnthropicCallError, AnthropicProvider
from factory.api import create_app
from factory.config import Settings
from factory.contracts import AgentOutput, CriterionResult, Plan, TaskSpec, Triage
from factory.dispatch import DemoRuntime, Dispatcher
from factory.engine import Supervisor
from factory.planning import validation_failures
from factory.registry import REGISTRY
from factory.store import AgentExecution, Outbox, Store, WorkRequest

FAKE_KEY = "test-only-not-a-real-anthropic-key"


@pytest.fixture
def ai_config(config):
    return config.model_copy(update={"mode": "anthropic", "anthropic_api_key": SecretStr(FAKE_KEY)})


@pytest.fixture
def provider(monkeypatch):
    state = SimpleNamespace(calls=[], count=100, status=200, stop="end_turn", invalid=False)

    def handler(request):
        assert request.url.host == "api.anthropic.com"
        assert request.headers["x-api-key"] == FAKE_KEY
        body = json.loads(request.content)
        assert "tools" not in body
        assert "cache_control" not in body
        state.calls.append((request.url.path, body))
        if state.status != 200:
            return httpx2.Response(
                state.status,
                json={
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": FAKE_KEY + " private upstream error",
                    },
                },
            )
        if request.url.path.endswith("count_tokens"):
            return httpx2.Response(200, json={"input_tokens": state.count})
        assert body["output_config"]["format"]["type"] == "json_schema"
        context = json.loads(body["messages"][0]["content"])
        agent = context["agent"]
        if agent == "triage":
            output = Triage(
                category="bug_fix", complexity="small", summary="Proposed fix", agents=["developer"]
            ).model_dump()
            output["usage"] = {"cost_usd": 999, "tokens": 999, "model": "made-up-by-model"}
        elif agent == "planner":
            output = Plan(
                rationale="Draft a proposed fix",
                tasks=[TaskSpec(id="fix", agent="developer", title="Propose fix")],
            ).model_dump()
        else:
            output = {
                "summary": "Draft for human review",
                "artifacts": [
                    {
                        "name": agent + ".md",
                        "type": "test_report",
                        "content": "Proposed tests and assumptions",
                        "location": "https://invented.example/evidence",
                        "sha256": "a" * 64,
                    }
                ],
            }
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [
                    {"type": "text", "text": "invalid JSON" if state.invalid else json.dumps(output)}
                ],
                "stop_reason": state.stop,
                "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 200},
            },
        )

    def client(**kwargs):
        assert kwargs["max_retries"] == 0
        assert kwargs["base_url"] == "https://api.anthropic.com"
        return AsyncAnthropic(
            **kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        )

    monkeypatch.setattr("factory.anthropic_provider.AsyncAnthropic", client)
    return state


def setup(config, work, mode="anthropic"):
    store = Store(config)
    store.create_schema()
    rid, _ = store.intake("demo", "operator", "anthropic-test", work, mode)
    with store.session() as session:
        payload = session.scalar(select(Outbox).where(Outbox.request_id == rid)).payload
    return store, rid, payload


def test_key_required_and_never_serialized(ai_config, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FACTORY_ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="requires ANTHROPIC_API_KEY"):
        Settings(mode="anthropic", _env_file=None)
    assert FAKE_KEY not in repr(ai_config)
    assert FAKE_KEY not in ai_config.model_dump_json()
    with TestClient(create_app(ai_config, dispatch=False)) as client:
        response = client.get("/api/config")
        assert response.json()["mode"] == "anthropic"
        assert response.json()["draft_only"]
        assert FAKE_KEY not in response.text
        agents = client.get("/api/agents").json()
        assert all(not agent["tools"] and not agent["permissions"] for agent in agents)
        assert not next(agent for agent in agents if agent["name"] == "deployment")["enabled"]


async def test_claude_uses_provider_usage_and_removes_unverified_evidence(ai_config, work, provider):
    result = await AnthropicProvider(ai_config).invoke(
        {"agent": "triage", "work": work.model_dump(mode="json")},
        REGISTRY["triage"].model_dump(),
    )
    assert result["usage"] == {"cost_usd": 0.0022, "tokens": 300, "model": "claude-sonnet-5"}
    result = await AnthropicProvider(ai_config).invoke(
        {"agent": "test", "work": work.model_dump(mode="json")},
        REGISTRY["test"].model_dump(),
    )
    assert result["draft"] and not result["passed"] and not result["simulated"]
    assert not result["tool_calls"]
    assert all(not criterion["passed"] and not criterion["evidence"] for criterion in result["criteria"])
    artifact = result["artifacts"][0]
    assert artifact["type"] == "analysis"
    assert artifact["location"] is None and artifact["sha256"] is None
    assert artifact["content"].startswith("UNEXECUTED AI DRAFT")


async def test_budget_rejected_before_paid_generation(ai_config, work, provider):
    provider.count = 1_000_000
    with pytest.raises(AnthropicCallError, match="allowance"):
        await AnthropicProvider(ai_config).invoke(
            {"agent": "triage", "work": work.model_dump(mode="json")},
            REGISTRY["triage"].model_dump(),
        )
    assert len(provider.calls) == 1 and provider.calls[0][0].endswith("count_tokens")


async def test_real_inference_path_finishes_as_draft_and_caches_results(ai_config, work, provider):
    store, rid, payload = setup(ai_config, work)
    runtime = DemoRuntime(store, ai_config, payload)
    result = await Supervisor(runtime, payload).run()
    assert result["status"] == "drafted"
    assert len(provider.calls) == 16  # Eight model calls, each preceded by token counting.
    with store.session() as session:
        row = session.get(WorkRequest, rid)
        assert row.status == "drafted" and row.completed_at
        assert row.cost == pytest.approx(8 * 0.0022)
        attempts = list(session.scalars(select(AgentExecution)))
        assert len(attempts) == 8
        assert all(FAKE_KEY not in json.dumps(a.output) for a in attempts)
    assert (await Supervisor(DemoRuntime(store, ai_config, payload), payload).run())["status"] == "drafted"
    assert len(provider.calls) == 16


@pytest.mark.parametrize("approved", [True, False])
async def test_draft_human_review_survives_restart_without_rebilling(ai_config, work, provider, approved):
    store, rid, payload = setup(ai_config, work)
    payload['draft_review_required'] = True
    runtime = DemoRuntime(store, ai_config, payload)

    async def interrupted(gate):
        assert gate == 'r0:policy-draft-review'
        raise asyncio.CancelledError()

    runtime.decision = interrupted
    with pytest.raises(asyncio.CancelledError):
        await Supervisor(runtime, payload).run()
    with store.session() as session:
        assert session.get(WorkRequest, rid).status == 'waiting_for_human'
    resumed = DemoRuntime(store, ai_config, payload)

    async def decide(gate):
        return {'approved': approved, 'comment': 'Human draft review'}

    resumed.decision = decide
    assert (await Supervisor(resumed, payload).run())['status'] == ('drafted' if approved else 'rejected')
    assert len(provider.calls) == 16


@pytest.mark.parametrize("failure", ["auth", "invalid", "truncated", "refusal"])
async def test_provider_failures_never_fall_back_or_leak_key(ai_config, work, provider, failure):
    if failure == "auth":
        provider.status = 401
    elif failure == "invalid":
        provider.invalid = True
    else:
        provider.stop = "max_tokens" if failure == "truncated" else "refusal"
    store, rid, payload = setup(ai_config, work)
    result = await Supervisor(DemoRuntime(store, ai_config, payload), payload).run()
    assert result["status"] == "failed" and FAKE_KEY not in json.dumps(result)
    assert len(provider.calls) == (1 if failure == "auth" else 2)
    with store.session() as session:
        attempt = session.scalar(select(AgentExecution))
        assert attempt.status == "failed" and FAKE_KEY not in attempt.error
        assert session.get(WorkRequest, rid).cost == (0 if failure == "auth" else 0.0022)


async def test_demo_work_cannot_turn_into_paid_inference(ai_config, work, provider):
    store, rid, payload = setup(ai_config, work, mode="demo")
    dispatcher = Dispatcher(store, ai_config)
    await dispatcher.tick()
    assert not dispatcher.tasks and not provider.calls
    with pytest.raises(PermanentAgentError, match="provider differs"):
        await AgentService(store, ai_config).invoke({**payload, "agent": "triage", "key": "triage:0"})
    assert not provider.calls
    await dispatcher.close()


def test_draft_mode_rejects_deployment_and_demo_seed(ai_config, work):
    with TestClient(create_app(ai_config, dispatch=False)) as client:
        work.deploy = True
        assert (
            client.post(
                "/api/requests", headers={"Idempotency-Key": "no-deployment"}, json=work.model_dump()
            ).status_code
            == 422
        )
        assert client.post("/api/demo/seed").status_code != 202


def test_draft_cannot_be_promoted_to_verified_completion(work):
    output = AgentOutput(
        summary="Draft",
        draft=True,
        passed=True,
        artifacts=[{"name": "draft.md", "type": "analysis", "content": "Draft"}],
        criteria=[
            CriterionResult(criterion=c, passed=True, evidence=["draft/draft.md"])
            for c in work.acceptance_criteria
        ],
    )
    failures = validation_failures(work, {"draft": output.model_dump()}, output, False)
    assert any("Unexecuted drafts" in failure for failure in failures)
