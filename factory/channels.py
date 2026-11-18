"""Connector envelope for trusted Teams, email and webhook normalization services."""

from typing import Literal

from pydantic import Field

from factory.contracts import Contract, WorkInput


class ChannelEnvelope(Contract):
    event_id: str = Field(min_length=1, max_length=160)
    subject: str = Field(min_length=5, max_length=180)
    body: str = Field(min_length=20, max_length=12000)
    sender: str = Field(min_length=2, max_length=200)
    repository: str = Field(min_length=1, max_length=200)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=30)
    priority: Literal["low", "normal", "high", "critical"] = "normal"
    business_context: str = Field(default="", max_length=4000)

    def normalize(self, channel: str) -> WorkInput:
        return WorkInput(
            title=self.subject,
            description=self.body,
            requestor=self.sender,
            source=channel,
            repository=self.repository,
            acceptance_criteria=self.acceptance_criteria,
            priority=self.priority,
            business_context=self.business_context,
        )
