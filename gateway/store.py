"""Operation receipts. A receipt is written before any work starts and is the answer to every repeat."""

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Operation(Base):
    __tablename__ = "gateway_operations"
    # tenant:request:execution key. One row per agent call, however often it is repeated.
    id: Mapped[str] = mapped_column(String(400), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200))
    request_id: Mapped[str] = mapped_column(String(80), index=True)
    key: Mapped[str] = mapped_column(String(120))
    agent: Mapped[str] = mapped_column(String(80))
    body_hash: Mapped[str] = mapped_column(String(64))
    # running | succeeded | failed (may be started again) | rejected (never started again)
    status: Mapped[str] = mapped_column(String(20), default="running")
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Every attempt's metered spend, including attempts that failed.
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Store:
    def __init__(self, url: str):
        options = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, pool_pre_ping=True, **options)
        self.session = sessionmaker(self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)

    def get(self, op_id: str) -> Operation | None:
        with self.session() as s:
            return s.get(Operation, op_id)

    def find(self, tenant_id: str, request_id: str, key: str) -> Operation | None:
        return self.get(f"{tenant_id}:{request_id}:{key}")

    def spent(self, request_id: str) -> float:
        with self.session() as s:
            total = s.scalar(select(func.sum(Operation.cost_usd)).where(Operation.request_id == request_id))
            return float(total or 0)

    def start(self, op_id: str, body: dict, body_hash: str) -> Operation:
        with self.session.begin() as s:
            op = s.get(Operation, op_id)
            if op is None:
                op = Operation(
                    id=op_id,
                    tenant_id=body["tenant_id"],
                    request_id=body["request_id"],
                    key=body["key"],
                    agent=body["agent"],
                    body_hash=body_hash,
                )
                s.add(op)
            else:
                op.status, op.error, op.completed_at = "running", None, None
                op.attempts += 1
            return op

    def finish(self, op_id: str, status: str, *, result=None, error=None, cost=0.0):
        with self.session.begin() as s:
            op = s.get(Operation, op_id)
            op.status, op.result, op.error, op.completed_at = status, result, error, now()
            op.cost_usd = (op.cost_usd or 0) + cost

    def interrupt_running(self) -> int:
        """Called at start-up: nothing can still be running in a process that has just begun."""
        with self.session.begin() as s:
            stale = list(s.scalars(select(Operation).where(Operation.status == "running")))
            for op in stale:
                op.status, op.error, op.completed_at = "failed", "Gateway stopped before this finished", now()
            return len(stale)
