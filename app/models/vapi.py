"""Pydantic v2 strict models for Vapi telephony webhooks and tool calls."""

import json
from enum import StrEnum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class ToolName(StrEnum):
    """Hardcoded tool routing table. Unmapped names fail closed."""

    CHECK_AVAILABILITY = "check_availability"
    BOOK_APPOINTMENT = "book_appointment"
    RECORD_DNC_REQUEST = "record_dnc_request"
    TRANSFER_CALL = "transfer_call"


class VapiFunctionCall(BaseModel):
    """Function call definition containing the target tool and its arguments."""

    model_config = ConfigDict(extra="forbid")

    name: ToolName
    arguments: dict[str, Any]

    @field_validator("arguments", mode="before")
    @classmethod
    def parse_arguments_if_str(cls, v: Any) -> Any:
        """Handle LLM argument polymorphism (JSON string vs parsed dict)."""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception as e:
                raise ValueError(f"Malformed JSON in tool arguments: {e}") from e
        return v


class VapiToolCall(BaseModel):
    """A discrete tool call dispatched by the Vapi assistant."""

    model_config = ConfigDict(extra="ignore")

    id: str
    type: str = "function"
    function: VapiFunctionCall


class VapiCustomer(BaseModel):
    """Metadata representing the inbound telephone caller."""

    model_config = ConfigDict(extra="ignore")

    number: str | None = None
    name: str | None = None


class VapiCallMetadata(BaseModel):
    """Call-level telephony metadata from Vapi orchestrator."""

    model_config = ConfigDict(extra="ignore")

    id: str
    customer: VapiCustomer = Field(default_factory=VapiCustomer)


class VapiWebhookMessage(BaseModel):
    """Inner message payload inside the inbound Vapi webhook."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    type: str
    tool_calls: list[VapiToolCall] = Field(
        default_factory=list,
        validation_alias=AliasChoices("toolCalls", "toolCallList", "tool_calls"),
        serialization_alias="toolCalls",
    )
    call: VapiCallMetadata | None = None


class VapiWebhookPayload(BaseModel):
    """Top-level inbound webhook payload delivered by Vapi."""

    model_config = ConfigDict(extra="ignore")

    message: VapiWebhookMessage


class VapiToolResultItem(BaseModel):
    """A single tool execution response spoken back to the caller."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    tool_call_id: str = Field(
        validation_alias=AliasChoices("toolCallId", "tool_call_id"),
        serialization_alias="toolCallId",
    )
    result: str  # Spoken natural language text (acoustic anti-markdown invariant)


class VapiWebhookResponse(BaseModel):
    """Outbound HTTP response returned to Vapi containing speech results."""

    model_config = ConfigDict(extra="forbid")

    results: list[VapiToolResultItem] = Field(default_factory=list)
