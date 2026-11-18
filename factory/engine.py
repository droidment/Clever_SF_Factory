"""Deterministic supervisor shared by Temporal and the explicitly non-production demo."""

import asyncio
from typing import Protocol

from factory.contracts import AgentOutput, Plan, TaskSpec, Triage, WorkInput
from factory.planning import compile_plan, ready_tasks, validation_failures


class Runtime(Protocol):
    cancelled: bool

    async def emit(self, key: str, type_: str, message: str, data: dict): ...
    async def invoke(self, payload: dict) -> dict: ...
    async def decision(self, gate: str) -> dict: ...


class RepairRequired(Exception):
    pass


class Supervisor:
    def __init__(self, runtime: Runtime, payload: dict):
        self.rt, self.payload = runtime, payload
        self.work = WorkInput.model_validate(payload["work"])
        self.cost = 0.0

    async def status(self, key, status, message, **data):
        await self.rt.emit(key, "workflow.status", message, {"status": status, **data})

    async def call(self, agent, key, **context):
        output = await self.rt.invoke(
            {
                "request_id": self.payload["request_id"],
                "tenant_id": self.payload["tenant_id"],
                "agent": agent,
                "key": key,
                "work": self.work.model_dump(mode="json"),
                **context,
            }
        )
        usage = output.get("usage", {})
        self.cost += usage.get("cost_usd", 0)
        await self.rt.emit(
            f"usage:{key}",
            "agent.completed",
            f"{agent.title()} returned a structured result",
            {"usage": usage, "agent": agent, "node_id": key},
        )
        return output

    def reserve(self, amount):
        if self.cost + amount > self.work.budget_usd:
            raise ValueError("Budget insufficient for the next batch, including retry allowance")

    async def run(self):
        try:
            return await self.execute()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.status("terminal-failure", "failed", f"Execution stopped: {str(exc)[:400]}")
            return {"status": "failed", "error": str(exc)[:400]}

    async def execute(self):
        for clarification_round in range(3):
            if await self.cancelled():
                return {"status": "cancelled"}
            await self.status(
                f"triaging:{clarification_round}", "triaging", "Assessing scope and execution risk"
            )
            self.reserve(2 if self.payload.get("draft_only") else 6)
            triage = Triage.model_validate(
                await self.call("triage", f"triage:{clarification_round}", max_cost_usd=2)
            )
            await self.rt.emit(
                f"triage-result:{clarification_round}",
                "triage.completed",
                triage.summary,
                {"triage": triage.model_dump(mode="json")},
            )
            if not triage.clarification and self.work.acceptance_criteria:
                break
            gate = f"clarification:{clarification_round}"
            await self.status(
                f"wait:{gate}",
                "waiting_for_human",
                triage.clarification or "Acceptance criteria are required",
                gate=gate,
            )
            decision = await self.rt.decision(gate)
            if await self.cancelled():
                return {"status": "cancelled"}
            if not decision["approved"]:
                await self.status("rejected", "rejected", "Request declined during clarification")
                return {"status": "rejected"}
            updated = self.work.model_dump(mode="json")
            updated["business_context"] = (
                updated["business_context"] + "\nClarification: " + decision["comment"]
            )[-4000:]
            if decision.get("acceptance_criteria"):
                updated["acceptance_criteria"] = decision["acceptance_criteria"]
            self.work = WorkInput.model_validate(updated)
            await self.rt.emit(
                f"clarified:{gate}", "request.clarified", "Human clarification recorded", {"work": updated}
            )
        else:
            raise ValueError("Clarification limit reached; operator intervention required")
        await self.status("planning", "planning", "Assembling a policy-checked execution graph")
        self.reserve(2 if self.payload.get("draft_only") else 6)
        proposal = Plan.model_validate(
            await self.call("planner", "planner", triage=triage.model_dump(mode="json"), max_cost_usd=2)
        )
        if self.payload.get("draft_only"):
            return await self.draft(proposal)
        repair_context = []
        for revision in range(3):
            if await self.cancelled():
                return {"status": "cancelled"}
            if revision:
                # Corrective work is bounded and re-runs all mandatory verification and approval gates.
                proposal = Plan(
                    rationale="Correct validation findings, then repeat verification.",
                    tasks=[
                        TaskSpec(
                            id="corrective-work",
                            agent="developer",
                            title="Correct failed acceptance criteria",
                        )
                    ],
                )
            plan = compile_plan(proposal, self.work, revision)
            await self.rt.emit(
                f"plan:{revision}",
                "plan.created",
                f"Execution plan revision {revision + 1} validated",
                {"plan": plan.model_dump(mode="json")},
            )
            outputs, completed = {}, set()
            await self.status(f"running:{revision}", "running", "Executing the dependency graph")
            try:
                while len(completed) < len(plan.tasks):
                    if await self.cancelled():
                        return {"status": "cancelled"}
                    ready = ready_tasks(plan, completed)[:4]
                    if not ready:
                        raise ValueError("No runnable nodes; graph cannot progress")
                    # Reserve the maximum possible spend across all concurrent attempts before starting them.
                    self.reserve(sum(t.max_cost_usd * 3 for t in ready if t.kind == "agent"))
                    results = await asyncio.gather(
                        *(self.run_node(t, revision, outputs, repair_context) for t in ready),
                        return_exceptions=True,
                    )
                    for task, result in zip(ready, results):
                        if isinstance(result, BaseException):
                            raise result
                        if result.get("rejected"):
                            await self.status(
                                "release-rejected", "rejected", "Release authorization declined"
                            )
                            return {"status": "rejected"}
                        if result.get("cancelled"):
                            await self.cancelled()
                            return {"status": "cancelled"}
                        completed.add(task.id)
                        if task.kind == "agent":
                            outputs[task.id] = result
                    if await self.cancelled():
                        return {"status": "cancelled"}
                    failed = [
                        f"{key}: {out.get('findings') or out['summary']}"
                        for key, out in outputs.items()
                        if not out.get("passed", False)
                    ]
                    if failed:
                        raise RepairRequired("; ".join(failed))
                final = AgentOutput.model_validate(outputs["policy-validation"])
                failures = validation_failures(self.work, outputs, final, self.payload["simulated"])
                if failures:
                    raise RepairRequired("; ".join(failures))
                status = "simulated" if self.payload["simulated"] else "completed"
                await self.status(
                    "complete",
                    status,
                    "Simulation finished — no code was delivered"
                    if status == "simulated"
                    else "Acceptance criteria verified; request completed",
                )
                return {"status": status, "cost_usd": self.cost}
            except RepairRequired as exc:
                repair_context.append(str(exc)[:4000])
                await self.rt.emit(
                    f"repair:{revision}",
                    "validation.correction_required",
                    str(exc)[:4000],
                    {"revision": revision, "findings": repair_context},
                )
                if revision == 2:
                    raise ValueError(
                        "Correction limit reached; unresolved findings require an operator"
                    ) from exc
        raise ValueError("Execution did not reach a terminal state")

    async def draft(self, proposal):
        if self.work.deploy:
            raise ValueError("Draft mode has no deployment tools")
        plan = compile_plan(proposal, self.work)
        for task in plan.tasks:
            task.title = {
                "test": "Draft a test plan (not executed)",
                "security": "Draft a security assessment (not scanned)",
                "review": "Review the proposed draft",
                "validation": "Summarize unverified acceptance criteria",
            }.get(task.agent, f"Draft only: {task.title}")
        if self.payload.get("draft_review_required"):
            plan.tasks.append(TaskSpec(id="policy-draft-review", agent="human", kind="approval",
                                       title="Review and approve AI drafts", dependencies=[t.id for t in plan.tasks]))
        await self.rt.emit(
            "plan:0",
            "plan.created",
            "Claude draft plan; no execution tools connected",
            {"plan": plan.model_dump(mode="json")},
        )
        outputs, completed = {}, set()
        await self.status("running:0", "running", "Generating AI drafts; code and tests are not executed")
        while len(completed) < len(plan.tasks):
            if await self.cancelled():
                return {"status": "cancelled"}
            ready = ready_tasks(plan, completed)[:4]
            if not ready:
                raise ValueError("Draft graph cannot progress")
            self.reserve(sum(task.max_cost_usd for task in ready if task.kind == "agent"))
            results = await asyncio.gather(
                *(self.run_node(task, 0, outputs, []) for task in ready),
                return_exceptions=True,
            )
            for task, result in zip(ready, results):
                if isinstance(result, BaseException):
                    raise result
                if task.kind == "approval":
                    if result.get("cancelled"):
                        await self.cancelled()
                        return {"status": "cancelled"}
                    if result.get("rejected"):
                        await self.status("draft-rejected", "rejected", "AI drafts declined by human reviewer")
                        return {"status": "rejected"}
                    completed.add(task.id)
                    continue
                if not result.get("draft") or result.get("tool_calls"):
                    raise ValueError("Draft provider violated its execution boundary")
                outputs[task.id] = result
                completed.add(task.id)
        if await self.cancelled():
            return {"status": "cancelled"}
        await self.status(
            "complete",
            "drafted",
            "AI drafts ready for review; no code, tests or deployments executed",
        )
        return {"status": "drafted", "cost_usd": self.cost}

    async def run_node(self, task, revision, outputs, repair_context):
        key = f"r{revision}:{task.id}"
        if task.kind == "approval":
            await self.rt.emit(
                f"task:{key}:waiting", "task.waiting", task.title, {"node_id": key, "task_status": "waiting"}
            )
            await self.status(f"gate:{key}", "waiting_for_human", task.title, gate=key)
            decision = await self.rt.decision(key)
            if self.rt.cancelled:
                return {"cancelled": True}
            await self.rt.emit(
                f"task:{key}:decision",
                "approval.decided",
                decision["comment"],
                {"node_id": key, "task_status": "completed" if decision["approved"] else "rejected"},
            )
            if decision["approved"]:
                await self.status(f"resume:{key}", "running", "Draft review approved; recording outcome" if self.payload.get("draft_only") else "Release authorized; resuming execution")
            return {"rejected": not decision["approved"]}
        await self.rt.emit(
            f"task:{key}:start",
            "task.started",
            task.title,
            {"node_id": key, "task_status": "running", "agent": task.agent},
        )
        if task.agent == "validation":
            await self.status(
                f"validating:{revision}",
                "validating",
                "Summarizing unverified acceptance criteria"
                if self.payload.get("draft_only")
                else "Checking original acceptance criteria against evidence",
            )
        try:
            # All produced artifacts are available, including dependencies behind a human gate.
            result = await self.call(
                task.agent,
                key,
                dependencies=outputs.copy(),
                repair_context=repair_context,
                max_cost_usd=task.max_cost_usd,
                release_gate=f"r{revision}:policy-approval",
            )
            await self.rt.emit(
                f"task:{key}:done",
                "task.completed",
                result["summary"],
                {
                    "node_id": key,
                    "task_status": "drafted"
                    if result.get("draft")
                    else "completed"
                    if result["passed"]
                    else "failed",
                },
            )
            return result
        except Exception:
            await self.rt.emit(
                f"task:{key}:failed",
                "task.failed",
                f"{task.title} exhausted its retry policy",
                {"node_id": key, "task_status": "failed"},
            )
            raise

    async def cancelled(self):
        if self.rt.cancelled:
            await self.status("cancelled", "cancelled", "Cancellation applied at the activity boundary")
            return True
        return False
