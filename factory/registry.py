from factory.contracts import Contract


class AgentDefinition(Contract):
    name: str
    responsibilities: str
    capabilities: list[str]
    tools: list[str]
    permissions: list[str]
    model_requirements: str = "structured-output, tenant-approved model"
    input_contract: str = "AgentInput/v1"
    output_contract: str = "AgentOutput/v1"
    prompt_version: str = "1.0.0"
    timeout_seconds: int = 180
    max_attempts: int = 3
    max_tokens: int = 16000
    enabled: bool = True


def definition(name, responsibility, tools, permissions, **kwargs):
    return AgentDefinition(
        name=name,
        responsibilities=responsibility,
        capabilities=[name],
        tools=tools,
        permissions=permissions,
        **kwargs,
    )


REGISTRY = {
    a.name: a
    for a in [
        definition(
            "triage", "Classify scope, risk, and missing information", [], [], output_contract="Triage/v1"
        ),
        definition("planner", "Propose a bounded execution DAG", [], [], output_contract="Plan/v1"),
        definition(
            "requirements",
            "Resolve requirements into verifiable criteria",
            ["repository.read"],
            ["repo:read"],
        ),
        definition("architecture", "Produce architecture decisions", ["repository.read"], ["repo:read"]),
        definition(
            "repository", "Inspect repository structure and dependencies", ["repository.read"], ["repo:read"]
        ),
        definition(
            "developer",
            "Implement scoped code changes",
            ["repository.read", "workspace.patch"],
            ["repo:read", "branch:write"],
        ),
        definition(
            "frontend",
            "Implement accessible user interfaces",
            ["repository.read", "workspace.patch"],
            ["repo:read", "branch:write"],
        ),
        definition(
            "backend",
            "Implement services and contracts",
            ["repository.read", "workspace.patch"],
            ["repo:read", "branch:write"],
        ),
        definition(
            "database",
            "Produce reversible database migrations",
            ["repository.read", "workspace.patch"],
            ["repo:read", "branch:write"],
        ),
        definition(
            "sap", "Analyze SAP integration contracts", ["repository.read"], ["repo:read"], enabled=False
        ),
        definition(
            "review", "Review patches for correctness", ["repository.read", "diff.read"], ["repo:read"]
        ),
        definition(
            "test", "Run isolated test suites and report evidence", ["sandbox.test"], ["sandbox:execute"]
        ),
        definition("security", "Scan dependencies and proposed changes", ["sandbox.scan"], ["sandbox:scan"]),
        definition(
            "performance",
            "Benchmark changes in an isolated environment",
            ["sandbox.benchmark"],
            ["sandbox:execute"],
        ),
        definition(
            "documentation",
            "Produce operational and user documentation",
            ["workspace.patch"],
            ["branch:write"],
        ),
        definition(
            "deployment", "Deploy an approved immutable artifact", ["release.deploy"], ["release:approved"]
        ),
        definition(
            "validation",
            "Verify every original acceptance criterion against evidence",
            ["artifact.read"],
            ["artifact:read"],
        ),
    ]
}


def require_agent(name: str) -> AgentDefinition:
    agent = REGISTRY.get(name)
    if not agent or not agent.enabled:
        raise ValueError(f"Agent is unavailable: {name}")
    return agent
