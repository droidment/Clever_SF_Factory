from sqlalchemy import func, select

from factory.auth import Principal, principal
from factory.store import Event, Outbox, WorkRequest


def post(client, work, key="test-request-1"):
    return client.post("/api/requests", headers={"Idempotency-Key": key}, json=work.model_dump(mode="json"))


def test_intake_is_atomic_idempotent_and_conflict_checked(client, app, work):
    first = post(client, work)
    assert first.status_code == 202
    assert first.json()["created"]
    second = post(client, work)
    assert second.json()["request_id"] == first.json()["request_id"]
    assert not second.json()["created"]
    changed = work.model_copy(update={"title": "Another request title"})
    assert post(client, changed).status_code == 409
    with app.state.store.session() as s:
        assert s.scalar(select(func.count()).select_from(WorkRequest)) == 1
        assert s.scalar(select(func.count()).select_from(Outbox)) == 1
        assert s.scalar(select(func.count()).select_from(Event)) == 1


def test_cross_tenant_access_and_repository_entitlement(client, app, work):
    request_id = post(client, work).json()["request_id"]
    app.dependency_overrides[principal] = lambda: Principal(
        "another-tenant", "reader", frozenset({"viewer"}), frozenset({work.repository})
    )
    assert client.get(f"/api/requests/{request_id}").status_code == 404
    assert client.get("/api/requests").json()["items"] == []
    assert client.get("/api/stats").json()["active_tasks"] == 0
    app.dependency_overrides[principal] = lambda: Principal(
        "demo", "reader", frozenset({"viewer"}), frozenset()
    )
    assert client.get(f"/api/requests/{request_id}").status_code == 404
    assert client.get("/api/requests").json()["items"] == []


def test_write_roles_and_repository_allowlist(client, app, work):
    app.dependency_overrides[principal] = lambda: Principal(
        "demo", "reader", frozenset({"viewer"}), frozenset({work.repository})
    )
    assert post(client, work).status_code == 403
    app.dependency_overrides.clear()
    assert post(client, work.model_copy(update={"repository": "private/forbidden"})).status_code == 403


def test_invalid_approval_and_immutable_decision(client, app, work):
    rid = post(client, work).json()["request_id"]
    body = {"gate": "r0:policy-approval", "approved": True, "comment": "Reviewed evidence"}
    assert client.post(f"/api/requests/{rid}/decisions", json=body).status_code == 409
    app.state.store.project(
        rid,
        "demo",
        "wait",
        "workflow.status",
        "Approve release",
        {"status": "waiting_for_human", "gate": body["gate"]},
    )
    assert client.post(f"/api/requests/{rid}/decisions", json=body).status_code == 202
    assert client.post(f"/api/requests/{rid}/decisions", json=body).json()["duplicate"]
    assert client.post(f"/api/requests/{rid}/decisions", json={**body, "approved": False}).status_code == 409


def test_projection_retries_are_idempotent(client, app, work):
    rid = post(client, work).json()["request_id"]
    for _ in range(3):
        app.state.store.project(
            rid, "demo", "usage:1", "agent.completed", "Done", {"usage": {"cost_usd": 0.23, "tokens": 42}}
        )
    detail = client.get(f"/api/requests/{rid}").json()
    assert detail["cost"] == 0.23
    assert detail["tokens"] == 42
    assert len(detail["events"]) == 2


def test_clarification_requires_valid_criteria(client, app, work):
    rid = post(client, work.model_copy(update={"acceptance_criteria": []})).json()["request_id"]
    app.state.store.project(
        rid,
        "demo",
        "wait",
        "workflow.status",
        "Clarify",
        {"status": "waiting_for_human", "gate": "clarification:0"},
    )
    body = {"gate": "clarification:0", "approved": True, "comment": "Proceed please"}
    assert client.post(f"/api/requests/{rid}/decisions", json=body).status_code == 422
    assert (
        client.post(
            f"/api/requests/{rid}/decisions", json={**body, "acceptance_criteria": ["Verified criterion"]}
        ).status_code
        == 202
    )


def test_intake_rate_limit_and_retry_exception(client, app, work):
    app.state.config.intake_per_minute = 1
    assert post(client, work).status_code == 202
    assert post(client, work).status_code == 202
    assert post(client, work, "different-key").status_code == 429


def test_cross_origin_mutation_rejected(client, work):
    response = client.post(
        "/api/requests",
        headers={"Origin": "https://attacker.example", "Idempotency-Key": "test-request"},
        json=work.model_dump(mode="json"),
    )
    assert response.status_code == 403
