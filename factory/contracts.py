"""Versioned contracts. Treat model and channel output as untrusted input."""

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class Status(StrEnum):
    QUEUED = "queued"
    TRIAGING = "triaging"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING = "waiting_for_human"
    VALIDATING = "validating"
    COMPLETED = "completed"
    SIMULATED = "simulated"
    DRAFTED = "drafted"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


TERMINAL = {Status.COMPLETED, Status.SIMULATED, Status.DRAFTED, Status.FAILED, Status.REJECTED, Status.CANCELLED}


class Attachment(Contract):
    name: str = Field(min_length=1, max_length=200)
    location: str = Field(pattern=r"^https://", max_length=2000)


class WorkInput(Contract):
    title: str = Field(min_length=5, max_length=180)
    description: str = Field(min_length=20, max_length=12000)
    requestor: str = Field(min_length=2, max_length=200)
    source: Literal["web", "api", "teams", "email", "webhook"] = "web"
    business_context: str = Field(default="", max_length=4000)
    repository: str = Field(min_length=1, max_length=200)
    priority: Literal["low", "normal", "high", "critical"] = "normal"
    due_date: date | None = None
    attachments: list[Attachment] = Field(default_factory=list, max_length=20)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=30)
    security_requirements: list[str] = Field(default_factory=list, max_length=20)
    deploy: bool = False
    budget_usd: float = Field(default=50, ge=1, le=10000)

    @model_validator(mode="after")
    def criteria_valid(self):
        for item in self.acceptance_criteria + self.security_requirements:
            if not item.strip() or len(item) > 1000:
                raise ValueError("Criteria must contain 1–1000 characters")
        self.acceptance_criteria = [x.strip() for x in self.acceptance_criteria]
        if len(set(self.acceptance_criteria)) != len(self.acceptance_criteria):
            raise ValueError("Acceptance criteria must be unique")
        return self


class Usage(Contract):
    cost_usd: float = Field(default=0, ge=0, le=10000)
    tokens: int = Field(default=0, ge=0, le=10000000)
    model: str = Field(default="deterministic-demo", max_length=200)


class Triage(Contract):
    category: Literal[
        "minor_task",
        "bug_fix",
        "enhancement",
        "refactoring",
        "investigation",
        "documentation",
        "test_automation",
        "infrastructure_change",
        "feature_development",
        "multi_system_implementation",
        "large_initiative",
    ]
    complexity: Literal["small", "medium", "large"]
    summary: str = Field(max_length=4000)
    agents: list[str] = Field(min_length=1, max_length=20)
    risks: list[str] = Field(default_factory=list, max_length=20)
    clarification: str | None = Field(default=None, max_length=2000)
    usage: Usage = Field(default_factory=Usage)


class TaskSpec(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,59}$")
    agent: str = Field(max_length=80)
    title: str = Field(min_length=1, max_length=200)
    dependencies: list[str] = Field(default_factory=list, max_length=32)
    kind: Literal["agent", "approval"] = "agent"
    max_cost_usd: float = Field(default=2, gt=0, le=10)


class Plan(Contract):
    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0, le=2)
    rationale: str = Field(max_length=4000)
    tasks: list[TaskSpec] = Field(min_length=1, max_length=32)
    usage: Usage = Field(default_factory=Usage)

    @model_validator(mode="after")
    def dag(self):
        ids = {task.id for task in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("Duplicate task IDs")
        done: set[str] = set()
        for task in self.tasks:
            if len(set(task.dependencies)) != len(task.dependencies):
                raise ValueError("Duplicate dependencies")
            if not set(task.dependencies) <= ids:
                raise ValueError("Unknown dependency")
        while len(done) < len(ids):
            ready = {t.id for t in self.tasks if t.id not in done and set(t.dependencies) <= done}
            if not ready:
                raise ValueError("Workflow graph contains a cycle")
            done |= ready
        return self


class ArtifactData(Contract):
    name: str = Field(min_length=1, max_length=200)
    type: Literal[
        "requirements",
        "architecture",
        "patch",
        "test_report",
        "review",
        "security_report",
        "documentation",
        "deployment",
        "validation",
        "analysis",
    ]
    content: str = Field(max_length=20000)
    location: str | None = Field(default=None, pattern=r"^https://", max_length=2000)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class CriterionResult(Contract):
    criterion: str = Field(min_length=1, max_length=1000)
    passed: bool
    evidence: list[str] = Field(default_factory=list, max_length=30)


class AgentOutput(Contract):
    summary: str = Field(min_length=1, max_length=4000)
    artifacts: list[ArtifactData] = Field(min_length=1, max_length=10)
    usage: Usage = Field(default_factory=Usage)
    simulated: bool = False
    draft: bool = False
    passed: bool = True
    criteria: list[CriterionResult] = Field(default_factory=list, max_length=30)
    findings: list[str] = Field(default_factory=list, max_length=30)
    decisions: list[str] = Field(default_factory=list, max_length=30)
    tool_calls: list[dict] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unique_artifacts(self):
        if len({a.name for a in self.artifacts}) != len(self.artifacts):
            raise ValueError("Artifact names must be unique within an agent output")
        return self


class HumanDecision(Contract):
    gate: str = Field(min_length=1, max_length=100)
    approved: bool
    comment: str = Field(min_length=3, max_length=2000)
    acceptance_criteria: list[str] | None = Field(default=None, max_length=30)
