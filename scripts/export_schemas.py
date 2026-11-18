import json
from pathlib import Path

from factory.channels import ChannelEnvelope
from factory.contracts import AgentOutput, HumanDecision, Plan, Triage, WorkInput
from factory.registry import AgentDefinition

target = Path(__file__).resolve().parent.parent / "schemas"
target.mkdir(exist_ok=True)
for contract in [WorkInput, Triage, Plan, AgentOutput, HumanDecision, AgentDefinition, ChannelEnvelope]:
    (target / f"{contract.__name__}.v1.json").write_text(
        json.dumps(contract.model_json_schema(), indent=2) + "\n", encoding="utf-8"
    )
print(f"Exported 7 versioned schemas to {target}")
