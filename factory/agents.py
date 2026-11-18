"""Agent boundary: audited, bounded, idempotent calls to a trusted execution gateway."""

import asyncio
import hashlib
import json

import httpx
from sqlalchemy import select

from factory.config import Settings
from factory.contracts import AgentOutput, ArtifactData, CriterionResult, Plan, Triage, WorkInput
from factory.planning import demo_proposal, demo_triage
from factory.registry import require_agent
from factory.store import AgentExecution, Artifact, Decision, Store, WorkflowExecution, WorkRequest, now

SCHEMAS = {"triage": Triage, "planner": Plan}
ARTIFACT_TYPES = {
    "architecture": "architecture",
    "developer": "patch",
    "frontend": "patch",
    "backend": "patch",
    "database": "patch",
    "test": "test_report",
    "review": "review",
    "security": "security_report",
    "documentation": "documentation",
    "deployment": "deployment",
    "validation": "validation",
}


class PermanentAgentError(Exception):
    pass


class AgentService:
    def __init__(self, store: Store, config: Settings):
        self.store, self.config = store, config

    async def invoke(self, payload: dict, attempt: int = 1) -> dict:
        name, key, request_id = payload["agent"], payload["key"], payload["request_id"]
        definition = require_agent(name)
        work = WorkInput.model_validate(payload["work"])
        if work.repository not in self.config.repositories:
            raise PermanentAgentError("Repository access denied")
        with self.store.session() as s:
            owner = s.get(WorkRequest, request_id)
            execution = s.get(WorkflowExecution, f"factory/{request_id}")
            if execution and (execution.engine == "anthropic") != (self.config.mode == "anthropic"):
                raise PermanentAgentError("Request provider differs from this runtime; refusing to change execution mode")
            if (
                not owner
                or owner.tenant_id != payload["tenant_id"]
                or owner.data["repository"] != work.repository
            ):
                raise PermanentAgentError("Activity tenant/repository does not match the stored request")
        if name == "deployment":
            with self.store.session() as s:
                decision = s.scalar(
                    select(Decision).where(
                        Decision.request_id == request_id, Decision.gate == payload.get("release_gate")
                    )
                )
                if not decision or not decision.data["approved"]:
                    raise PermanentAgentError(
                        "Deployment requires an approved decision for this plan revision"
                    )
        with self.store.session.begin() as s:
            cached = s.scalar(
                select(AgentExecution).where(
                    AgentExecution.request_id == request_id,
                    AgentExecution.execution_key == key,
                    AgentExecution.status == "completed",
                )
            )
            if cached:
                return cached.output
            record = s.scalar(
                select(AgentExecution).where(
                    AgentExecution.request_id == request_id,
                    AgentExecution.execution_key == key,
                    AgentExecution.attempt == attempt,
                )
            )
            if record is None:
                record = AgentExecution(
                    request_id=request_id, execution_key=key, agent=name, attempt=attempt, input=payload
                )
                s.add(record)
                s.flush()
            record_id = record.id
        try:
            if self.config.mode == "demo":
                await asyncio.sleep(self.config.demo_delay)
                output = self.simulate(name, work, payload)
            elif self.config.mode == "anthropic":
                from factory.anthropic_provider import AnthropicProvider

                output = await AnthropicProvider(self.config).invoke(payload, definition.model_dump())
            else:
                output = await self.gateway(payload, definition.model_dump())
            model = SCHEMAS.get(name, AgentOutput)
            parsed = model.model_validate(output)
            if parsed.usage.cost_usd > payload.get("max_cost_usd", 2):
                raise PermanentAgentError("Gateway exceeded the authorized call budget")
            if self.config.mode != "demo" and getattr(parsed, "simulated", False):
                raise PermanentAgentError("Production gateway returned simulated evidence")
            if self.config.mode == "temporal" and getattr(parsed, "draft", False):
                raise PermanentAgentError("Unexecuted drafts cannot establish production completion")
            output = parsed.model_dump(mode="json")
            if len(json.dumps(output).encode()) > 128000:
                raise PermanentAgentError("Result exceeds 128 KB; store large artifacts externally")
            with self.store.session.begin() as s:
                record = s.get(AgentExecution, record_id)
                record.output, record.status, record.completed_at = output, "completed", now()
                for artifact in output.get("artifacts", []):
                    artifact["sha256"] = hashlib.sha256(artifact["content"].encode()).hexdigest()
                    existing = s.scalar(
                        select(Artifact).where(
                            Artifact.request_id == request_id,
                            Artifact.node_id == key,
                            Artifact.name == artifact["name"],
                        )
                    )
                    if not existing:
                        s.add(
                            Artifact(request_id=request_id, node_id=key, name=artifact["name"], data=artifact)
                        )
            return output
        except asyncio.CancelledError:
            with self.store.session.begin() as s:
                record = s.get(AgentExecution, record_id)
                record.status, record.error, record.completed_at = "cancelled", "Execution cancelled", now()
            raise
        except Exception as exc:
            with self.store.session.begin() as s:
                record = s.get(AgentExecution, record_id)
                record.status, record.error, record.completed_at = (
                    "failed",
                    type(exc).__name__ + ": " + str(exc)[:500],
                    now(),
                )
                if getattr(exc, "usage", None):
                    record.output = {"usage": exc.usage}
            if getattr(exc, "usage", None):
                self.store.project(
                    request_id, payload["tenant_id"], f"failed-usage:{key}:{attempt}", "agent.failed_usage",
                    "Provider usage recorded for an unusable response", {"usage": exc.usage},
                )
            raise

    async def gateway(self, payload, definition):
        body = {
            "schema_version": 1,
            **payload,
            "definition": definition,
            "output_schema": SCHEMAS.get(payload["agent"], AgentOutput).model_json_schema(),
        }
        # Fixed admin-controlled endpoint. Request text cannot select credentials, URL, or tools.
        async with httpx.AsyncClient(timeout=165, follow_redirects=False) as client:
            async with client.stream(
                "POST",
                self.config.gateway_url,
                json=body,
                headers={
                    "Authorization": f"Bearer {self.config.gateway_token}",
                    "Idempotency-Key": f"{payload['request_id']}:{payload['key']}",
                },
            ) as response:
                if response.status_code in {400, 401, 403, 404, 422}:
                    raise PermanentAgentError(f"Gateway rejected request: HTTP {response.status_code}")
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 256000:
                        raise PermanentAgentError("Gateway response too large")
                    chunks.append(chunk)
        return json.loads(b"".join(chunks))

    def simulate(self, name, work, payload):
        if name == "triage":
            return demo_triage(work).model_dump(mode="json")
        if name == "planner":
            return demo_proposal(work, Triage.model_validate(payload["triage"])).model_dump(mode="json")
        refs = [
            f"{k}/{a['name']}"
            for k, output in payload.get("dependencies", {}).items()
            for a in output.get("artifacts", [])
        ]
        content = f"# {name.title()} demonstration\n\nRequest: {work.title}\nRepository: {work.repository}\n\nThis is a simulated artifact. No repository, model, build system, or deployment was accessed.\n\n"
        content += "Acceptance criteria:\n" + "\n".join(f"- {c}" for c in work.acceptance_criteria)
        return AgentOutput(
            summary=f"{name.title()} simulation finished; no external tools executed.",
            simulated=True,
            artifacts=[
                ArtifactData(
                    name=f"{name}-report.md", type=ARTIFACT_TYPES.get(name, "analysis"), content=content
                )
            ],
            criteria=[
                CriterionResult(criterion=c, passed=True, evidence=refs[:30])
                for c in work.acceptance_criteria
            ]
            if name == "validation"
            else [],
            decisions=["Demonstration mode: completion will be marked simulated."],
        ).model_dump(mode="json")
