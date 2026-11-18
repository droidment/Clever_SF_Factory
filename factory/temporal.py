"""The workflow imports only deterministic orchestration code, never DB/HTTP clients."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from factory.engine import Supervisor


@workflow.defn(name="SoftwareFactoryV1")
class FactoryWorkflow:
    def __init__(self):
        self.decisions = {}
        self.cancelled = False
        self.payload = {}
        self.current_status = "queued"

    @workflow.signal
    def human_decision(self, decision: dict):
        # API guarantees one decision per gate. Redelivery must not overwrite it.
        self.decisions.setdefault(decision["gate"], decision)

    @workflow.signal
    def request_cancel(self):
        self.cancelled = True

    @workflow.query
    def state(self) -> dict:
        return {"status": self.current_status, "cancel_requested": self.cancelled}

    @workflow.run
    async def run(self, payload: dict) -> dict:
        self.payload = payload
        return await Supervisor(self, payload).run()

    async def emit(self, key, type_, message, data):
        self.current_status = data.get("status", self.current_status)
        await workflow.execute_activity(
            "project",
            {
                "request_id": self.payload["request_id"],
                "tenant_id": self.payload["tenant_id"],
                "key": key,
                "type_": type_,
                "message": message,
                "data": data,
            },
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1), maximum_interval=timedelta(minutes=1)
            ),
        )

    async def invoke(self, payload):
        return await workflow.execute_activity(
            "invoke_agent",
            payload,
            start_to_close_timeout=timedelta(minutes=3),
            schedule_to_close_timeout=timedelta(minutes=12),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                initial_interval=timedelta(seconds=2),
                backoff_coefficient=2,
                non_retryable_error_types=["PermanentAgentError", "ValidationError", "ValueError"],
            ),
        )

    async def decision(self, gate):
        await workflow.wait_condition(lambda: gate in self.decisions or self.cancelled)
        return self.decisions.get(gate, {"approved": False, "comment": "Cancelled"})
