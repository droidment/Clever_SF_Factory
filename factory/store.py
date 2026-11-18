"""Transactional intake outbox and idempotent operational projections."""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from factory.config import Settings
from factory.contracts import TERMINAL, WorkInput


def now():
    return datetime.now(UTC)


def uid():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class WorkRequest(Base):
    __tablename__ = "work_requests"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    payload_hash: Mapped[str] = mapped_column(String(64))
    submitted_by: Mapped[str] = mapped_column(String(200))
    data: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(200), default="Awaiting dispatch")
    triage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cost: Mapped[float] = mapped_column(Float, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    gate: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowExecution(Base):
    __tablename__ = "workflow_executions"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("work_requests.id"), unique=True)
    engine: Mapped[str] = mapped_column(String(20))
    version: Mapped[str] = mapped_column(String(20), default="v1")
    status: Mapped[str] = mapped_column(String(40), default="queued")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("request_id", "node_id"),)
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("work_requests.id"), index=True)
    node_id: Mapped[str] = mapped_column(String(100))
    spec: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentExecution(Base):
    __tablename__ = "agent_executions"
    __table_args__ = (UniqueConstraint("request_id", "execution_key", "attempt"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    request_id: Mapped[str] = mapped_column(ForeignKey("work_requests.id"), index=True)
    execution_key: Mapped[str] = mapped_column(String(100))
    agent: Mapped[str] = mapped_column(String(80))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), default="running")
    input: Mapped[dict] = mapped_column(JSON)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("request_id", "node_id", "name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    request_id: Mapped[str] = mapped_column(ForeignKey("work_requests.id"), index=True)
    node_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    data: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("request_id", "event_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("work_requests.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    event_key: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (UniqueConstraint("request_id", "command_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    request_id: Mapped[str] = mapped_column(ForeignKey("work_requests.id"), index=True)
    command_key: Mapped[str] = mapped_column(String(150))
    type: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON)
    delivered: Mapped[bool] = mapped_column(default=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (UniqueConstraint("request_id", "gate"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    request_id: Mapped[str] = mapped_column(ForeignKey("work_requests.id"))
    gate: Mapped[str] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(200))
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ExecutionLease(Base):
    """Scheduling state for the Postgres runtime: who is running a request, and when to resume it."""

    __tablename__ = "execution_leases"
    request_id: Mapped[str] = mapped_column(ForeignKey("work_requests.id"), primary_key=True)
    # When the request may next be claimed. NULL means parked until a command arrives.
    wake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Incremented for every delivered command, so a run can tell that one arrived while it was working.
    signals: Mapped[int] = mapped_column(Integer, default=0)
    # Consecutive claims that ended without progress; bounds a request that keeps stopping its worker.
    claims: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(default=False)


class Store:
    def __init__(self, config: Settings):
        if config.database_url.startswith("sqlite:///./data/"):
            Path("data").mkdir(exist_ok=True)
        self.engine = create_engine(
            config.database_url,
            connect_args={"check_same_thread": False, "timeout": 20}
            if config.database_url.startswith("sqlite")
            else {},
            pool_pre_ping=True,
        )
        if config.database_url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def pragmas(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")

        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def create_schema(self):
        Base.metadata.create_all(self.engine)

    def intake(self, tenant: str, actor: str, key: str, work: WorkInput, mode: str, *, draft_review_required=False):
        payload = work.model_dump(mode="json")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.session.begin() as s:
            existing = s.scalar(
                select(WorkRequest).where(WorkRequest.tenant_id == tenant, WorkRequest.idempotency_key == key)
            )
            if existing:
                if existing.payload_hash != digest:
                    raise ValueError("Idempotency key already used with a different request")
                return existing.id, False
            request_id = uid()
            s.add(
                WorkRequest(
                    id=request_id,
                    tenant_id=tenant,
                    submitted_by=actor,
                    idempotency_key=key,
                    payload_hash=digest,
                    data=payload,
                )
            )
            s.flush()
            s.add(WorkflowExecution(id=f"factory/{request_id}", request_id=request_id, engine=mode))
            s.add(
                Outbox(
                    request_id=request_id,
                    command_key="start",
                    type="start",
                    payload={
                        "request_id": request_id,
                        "tenant_id": tenant,
                        "work": payload,
                        "simulated": mode == "demo",
                        "draft_only": mode == "anthropic",
                        "draft_review_required": mode == "anthropic" and draft_review_required,
                    },
                )
            )
            s.add(
                Event(
                    request_id=request_id,
                    tenant_id=tenant,
                    event_key="intake",
                    type="request.received",
                    message="Request accepted and durably queued",
                    data={"actor": actor, "source": work.source},
                )
            )
            return request_id, True

    def project(self, request_id: str, tenant: str, key: str, type_: str, message: str, data: dict):
        with self.session.begin() as s:
            # Serializes projection updates per request on PostgreSQL; SQLite serializes writers.
            work = s.scalar(
                select(WorkRequest)
                .where(WorkRequest.id == request_id, WorkRequest.tenant_id == tenant)
                .with_for_update()
            )
            if work is None:
                raise ValueError("Unknown request")
            if s.scalar(select(Event.id).where(Event.request_id == request_id, Event.event_key == key)):
                return
            s.add(
                Event(
                    request_id=request_id,
                    tenant_id=tenant,
                    event_key=key,
                    type=type_,
                    message=message,
                    data=data,
                )
            )
            work.updated_at = now()
            if "status" in data:
                work.status, work.stage = data["status"], message
                work.gate = data.get("gate")
                execution = s.get(WorkflowExecution, f"factory/{request_id}")
                execution.status = work.status
                execution.started_at = execution.started_at or now()
                if work.status in TERMINAL:
                    work.completed_at = now()
                    execution.completed_at = work.completed_at
            if "triage" in data:
                work.triage = data["triage"]
            if "work" in data:
                work.data = data["work"]
            if "plan" in data:
                work.plan = data["plan"]
                for node in work.plan["tasks"]:
                    node_id = f"r{work.plan['revision']}:{node['id']}"
                    s.add(
                        Task(id=f"{request_id}:{node_id}", request_id=request_id, node_id=node_id, spec=node)
                    )
            if "node_id" in data and "task_status" in data:
                task = s.get(Task, f"{request_id}:{data['node_id']}")
                if task:
                    task.status = data["task_status"]
                    if task.status == "running":
                        task.started_at = task.started_at or now()
                    if task.status in {"completed", "drafted", "failed", "rejected"}:
                        task.completed_at = now()
            if "usage" in data:
                work.cost = WorkRequest.cost + data["usage"].get("cost_usd", 0)
                work.tokens = WorkRequest.tokens + data["usage"].get("tokens", 0)


def as_dict(record):
    return {column.name: getattr(record, column.name) for column in record.__table__.columns}
