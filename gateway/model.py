"""Claude calls for the gateway: one structured call, or a tool loop over confined file tools.

Spend is taken from the usage block of every response the API returns, never from model text.
"""

import asyncio
import json

from anthropic import APIError, APIStatusError, AsyncAnthropic, transform_schema
from pydantic import BaseModel, ValidationError

from gateway.config import RATES, Settings
from gateway.workspace import Files, ToolError, cut


class Rejected(Exception):
    """Will fail the same way if repeated. Reported to the factory as a permanent failure."""


class ModelError(Exception):
    """Worth another attempt."""


class Meter:
    def __init__(self, model: str, ceiling: float):
        self.model, self.ceiling = model, ceiling
        self.cost = self.last = 0.0
        self.tokens = 0

    def add(self, usage):
        rate_in, rate_out, rate_read = RATES[self.model]
        written = getattr(usage, "cache_creation_input_tokens", None) or 0
        read = getattr(usage, "cache_read_input_tokens", None) or 0
        self.last = (
            usage.input_tokens * rate_in + written * rate_in * 1.25 + read * rate_read
            + usage.output_tokens * rate_out
        ) / 1_000_000
        self.cost += self.last
        self.tokens += usage.input_tokens + written + read + usage.output_tokens

    def room(self):
        """Stop before a turn that would probably cross the authorised limit for this call."""
        if self.cost + max(self.last * 1.5, 0.02) > self.ceiling:
            raise Rejected(f"Stopped at ${self.cost:.2f}: the next model turn could exceed the ${self.ceiling:.2f} limit")

    def usage(self) -> dict:
        return {
            "cost_usd": round(self.cost, 6),
            "tokens": self.tokens,
            "model": self.model if self.tokens else "none",
        }


class Model:
    def __init__(self, config: Settings):
        self.config = config

    def client(self) -> AsyncAnthropic:
        # The endpoint is fixed so an inherited ANTHROPIC_BASE_URL cannot redirect the key.
        return AsyncAnthropic(
            api_key=self.config.anthropic_api_key.get_secret_value(),
            base_url="https://api.anthropic.com",
            max_retries=2,
            timeout=600,
        )

    async def _create(self, client, meter: Meter, output_format=None, **request):
        meter.room()
        output_config = {"effort": self.config.effort}
        if output_format:
            output_config["format"] = output_format
        try:
            response = await client.messages.create(
                model=self.config.model,
                max_tokens=16000,
                output_config=output_config,
                cache_control={"type": "ephemeral"},
                **request,
            )
        except APIStatusError as exc:
            # SDK error bodies can echo input or credentials; keep only the status.
            kind = Rejected if exc.status_code in {400, 401, 403, 404} else ModelError
            raise kind(f"Anthropic HTTP {exc.status_code}") from None
        except APIError:
            raise ModelError("Anthropic connection failed or timed out") from None
        meter.add(response.usage)
        if response.stop_reason == "refusal":
            raise Rejected("The model declined this request")
        if response.stop_reason == "max_tokens":
            raise ModelError("The model's output was cut off at the token limit")
        return response

    async def structured(self, system: str, content: str, output_type: type[BaseModel], meter: Meter):
        """One call, no tools, output constrained to a schema."""
        if not self.config.anthropic_api_key.get_secret_value().strip():
            raise Rejected("The gateway has no Anthropic API key configured")
        schema = {"type": "json_schema", "schema": transform_schema(output_type.model_json_schema())}
        async with self.client() as client:
            response = await self._create(
                client, meter, output_format=schema, system=system, messages=[{"role": "user", "content": content}]
            )
        try:
            return output_type.model_validate_json("".join(b.text for b in response.content if b.type == "text"))
        except (ValidationError, ValueError):
            raise ModelError("The model returned output that does not match the contract") from None

    async def loop(self, system: str, content: str, files: Files, result_type: type[BaseModel], meter: Meter):
        """Let the model use the file tools until it submits a result of the given shape."""
        if not self.config.anthropic_api_key.get_secret_value().strip():
            raise Rejected("The gateway has no Anthropic API key configured")
        tools = files.tools + [
            {
                "name": "submit",
                "description": "Finish the task and hand in the result. Call this exactly once, last.",
                "input_schema": result_type.model_json_schema(),
            }
        ]
        messages = [{"role": "user", "content": content}]
        reminded = False
        async with self.client() as client:
            for _ in range(self.config.max_turns):
                response = await self._create(client, meter, system=system, tools=tools, messages=messages)
                # Returned unchanged, thinking blocks included: the API requires an append-only history.
                messages.append({"role": "assistant", "content": response.content})
                calls = [block for block in response.content if block.type == "tool_use"]
                if not calls:
                    if reminded:
                        raise ModelError("The model stopped without submitting a result")
                    reminded = True
                    messages.append({"role": "user", "content": "Call the submit tool with your result to finish."})
                    continue
                results, final, failed = [], None, False
                for call in calls:
                    try:
                        if call.name == "submit":
                            final = result_type.model_validate(call.input)
                            text = "Result received."
                        else:
                            text = await asyncio.to_thread(files.run, call.name, call.input)
                        results.append({"type": "tool_result", "tool_use_id": call.id, "content": cut(text, 60000)})
                    except (ToolError, ValidationError) as exc:
                        failed = True
                        detail = (
                            json.dumps(exc.errors(include_url=False, include_input=False))[:2000]
                            if isinstance(exc, ValidationError)
                            else str(exc)
                        )
                        results.append(
                            {"type": "tool_result", "tool_use_id": call.id, "content": detail, "is_error": True}
                        )
                if final is not None and not failed:
                    return final
                messages.append({"role": "user", "content": results})
        raise ModelError(f"No result after {self.config.max_turns} model turns")
