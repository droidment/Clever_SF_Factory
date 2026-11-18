import pytest
from pydantic import ValidationError

from factory.contracts import AgentOutput, ArtifactData, CriterionResult, Plan, TaskSpec
from factory.planning import compile_plan, demo_proposal, demo_triage, ready_tasks, validation_failures


@pytest.mark.parametrize(
    "tasks",
    [
        [{"id": "a", "agent": "developer", "title": "A", "dependencies": ["b"]}],
        [{"id": "a", "agent": "developer", "title": "A", "dependencies": ["a"]}],
        [{"id": "a", "agent": "developer", "title": "A"}, {"id": "a", "agent": "developer", "title": "B"}],
        [
            {"id": "a", "agent": "developer", "title": "A", "dependencies": ["b"]},
            {"id": "b", "agent": "developer", "title": "B", "dependencies": ["a"]},
        ],
    ],
)
def test_invalid_dags_rejected(tasks):
    with pytest.raises(ValidationError):
        Plan(rationale="Unsafe graph", tasks=tasks)


def test_policy_cannot_be_bypassed(work):
    for agent in ["deployment", "human", "unknown", "sap"]:
        with pytest.raises(ValueError):
            compile_plan(
                Plan(
                    rationale="Attempt bypass",
                    tasks=[TaskSpec(id="unsafe", agent=agent, title="Run unsafe work")],
                ),
                work,
            )


def test_dynamic_plan_and_release_dependencies(work):
    work.deploy = True
    work.description = "Implement a platform migration with frontend, backend and database changes."
    plan = compile_plan(demo_proposal(work, demo_triage(work)), work)
    by_id = {t.id: t for t in plan.tasks}
    assert {"backend", "frontend", "database"} <= set(by_id)
    assert {t.id for t in ready_tasks(plan, {"architecture"})} == {"backend", "frontend", "database"}
    assert by_id["policy-deploy"].dependencies == ["policy-approval"]
    assert {"policy-test", "policy-review", "policy-security", "policy-docs"} <= set(
        by_id["policy-approval"].dependencies
    )
    assert "policy-deploy" in by_id["policy-validation"].dependencies


def test_completion_requires_exact_acceptance_and_real_evidence(work):
    output = AgentOutput(
        summary="Done",
        artifacts=[ArtifactData(name="report", type="validation", content="A report")],
        criteria=[],
    )
    assert validation_failures(work, {}, output, False)
    output.criteria = [
        CriterionResult(criterion=c, passed=True, evidence=["test/report"]) for c in work.acceptance_criteria
    ]
    outputs = {"test": {"passed": True, "simulated": False, "artifacts": [{"name": "report"}]}}
    assert validation_failures(work, outputs, output, False) == []
    outputs["test"]["simulated"] = True
    assert validation_failures(work, outputs, output, False)
    assert validation_failures(work, outputs, output, True) == []
    output.criteria[0].evidence = ["invented/evidence"]
    assert validation_failures(work, outputs, output, True)
