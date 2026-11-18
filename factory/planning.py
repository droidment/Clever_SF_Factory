"""Pure plan compilation: model proposals never grant themselves permissions."""

from factory.contracts import AgentOutput, Plan, TaskSpec, Triage, WorkInput
from factory.registry import require_agent

IMPLEMENTERS = {
    "requirements",
    "architecture",
    "repository",
    "developer",
    "frontend",
    "backend",
    "database",
    "performance",
    "documentation",
}


def demo_triage(work: WorkInput) -> Triage:
    text = (work.title + " " + work.description).lower()
    large = any(word in text for word in ["platform", "multi-system", "initiative", "migration"])
    feature = any(word in text for word in ["feature", "dashboard", "portal", "frontend", "interface"])
    category = (
        "large_initiative"
        if large
        else "feature_development"
        if feature
        else "bug_fix"
        if "bug" in text or "fix" in text
        else "enhancement"
    )
    agents = (
        ["backend", "frontend", "database"]
        if large
        else ["backend", "frontend"]
        if feature
        else ["developer"]
    )
    if "documentation" in text and not feature and not large:
        category, agents = "documentation", ["documentation"]
    return Triage(
        category=category,
        complexity="large" if large else "medium" if feature else "small",
        summary="Demonstration classification based on request scope. No model was called.",
        agents=agents,
        risks=["Production release requires a human decision"] if work.deploy else [],
        clarification=None
        if work.acceptance_criteria
        else "Provide measurable acceptance criteria before execution.",
    )


def demo_proposal(work: WorkInput, triage: Triage) -> Plan:
    tasks = []
    if triage.complexity != "small":
        tasks.append(TaskSpec(id="architecture", agent="architecture", title="Design the implementation"))
    for agent in triage.agents:
        tasks.append(
            TaskSpec(
                id=agent,
                agent=agent,
                title=f"{agent.title()} implementation",
                dependencies=["architecture"] if tasks and tasks[0].id == "architecture" else [],
            )
        )
    return Plan(
        rationale="Scope-selected specialists, followed by mandatory verification and release controls.",
        tasks=tasks,
    )


def compile_plan(proposal: Plan, work: WorkInput, revision: int = 0) -> Plan:
    if len(proposal.tasks) > 20:
        raise ValueError("At most 20 implementation tasks per plan")
    tasks = []
    for node in proposal.tasks:
        require_agent(node.agent)
        if node.agent not in IMPLEMENTERS or node.kind != "agent" or node.id.startswith("policy-"):
            raise ValueError(
                "Planner may only propose implementation tasks; policy owns verification and release"
            )
        tasks.append(node.model_copy(deep=True))
    all_ids = [t.id for t in tasks]
    for agent, title in [
        ("test", "Execute test suite"),
        ("review", "Review implementation"),
        ("security", "Verify security controls"),
    ]:
        tasks.append(TaskSpec(id=f"policy-{agent}", agent=agent, title=title, dependencies=all_ids))
    checks = ["policy-test", "policy-review", "policy-security"]
    tasks.append(
        TaskSpec(id="policy-docs", agent="documentation", title="Document the change", dependencies=all_ids)
    )
    checks.append("policy-docs")
    if work.deploy:
        tasks.append(
            TaskSpec(
                id="policy-approval",
                agent="human",
                kind="approval",
                title="Authorize production release",
                dependencies=checks,
            )
        )
        tasks.append(
            TaskSpec(
                id="policy-deploy",
                agent="deployment",
                title="Deploy approved artifact",
                dependencies=["policy-approval"],
            )
        )
    tasks.append(
        TaskSpec(
            id="policy-validation",
            agent="validation",
            title="Validate acceptance criteria",
            dependencies=[t.id for t in tasks],
        )
    )
    return Plan(revision=revision, rationale=proposal.rationale, tasks=tasks, usage=proposal.usage)


def ready_tasks(plan: Plan, completed: set[str]) -> list[TaskSpec]:
    return [t for t in plan.tasks if t.id not in completed and set(t.dependencies) <= completed]


def validation_failures(
    work: WorkInput, outputs: dict[str, dict], final: AgentOutput, simulated: bool
) -> list[str]:
    failures = []
    expected = set(work.acceptance_criteria)
    actual = {c.criterion for c in final.criteria}
    if not expected or actual != expected or len(final.criteria) != len(expected):
        failures.append("Validation must cover every original acceptance criterion exactly once")
    evidence = {
        f"{task_id}/{artifact['name']}"
        for task_id, out in outputs.items()
        if task_id != "policy-validation"
        for artifact in out.get("artifacts", [])
    }
    for criterion in final.criteria:
        if not criterion.passed or not criterion.evidence or not set(criterion.evidence) <= evidence:
            failures.append(f"Missing or failing evidence: {criterion.criterion}")
    for key, out in outputs.items():
        if not out.get("passed", False):
            failures.append(f"Verification failed: {key}")
        if out.get("simulated", False) and not simulated:
            failures.append(f"Simulated evidence cannot establish completion: {key}")
        if out.get("draft", False):
            failures.append(f"Unexecuted drafts cannot establish completion: {key}")
    if not final.passed:
        failures.extend(final.findings or ["Final validation failed"])
    return list(dict.fromkeys(failures))
