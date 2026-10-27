"""Pydantic v2 domain models for appointment operations and caller inputs."""

import re
from datetime import datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

# SECURITY-WAIVER: regex on bounded untrusted input per agent-security.md §2
_ANSI_ESCAPE_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class CheckAvailabilityInput(BaseModel):
    """Validated input arguments for the check_availability tool."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    start_time: str = Field(
        validation_alias=AliasChoices("start_time", "start_date"),
        description="Date or ISO start timestamp (e.g. '2026-10-28')",
        max_length=50,
    )
    end_time: str | None = Field(
        default=None,
        validation_alias=AliasChoices("end_time", "end_date"),
        description="Optional ISO end date or timestamp",
        max_length=50,
    )
    timezone: str = Field(default="America/New_York", max_length=50)


class BookAppointmentInput(BaseModel):
    """Validated input arguments for the book_appointment tool."""

    model_config = ConfigDict(extra="forbid")

    customer_name: str = Field(
        min_length=2,
        max_length=100,
        description="Full spoken name of caller",
    )
    phone: str = Field(
        min_length=7,
        max_length=25,
        description="Caller phone number (E.164 or national format)",
    )
    start_time: str = Field(
        max_length=50,
        description="ISO-8601 start timestamp of slot",
    )
    service_type: str = Field(
        default="General Consultation",
        max_length=100,
    )
    idempotency_key: str | None = Field(default=None, max_length=128)

    @field_validator("customer_name")
    @classmethod
    def sanitize_name(cls, v: str) -> str:
        """Strip newlines, null bytes, and ANSI escape sequences (LLM02)."""
        stripped_ansi = _ANSI_ESCAPE_RE.sub("", v)
        clean = (
            stripped_ansi.replace("\n", " ")
            .replace("\r", " ")
            .replace("\0", "")
            .strip()
        )
        if not clean:
            raise ValueError("Customer name cannot be empty")
        return clean

    @field_validator("phone")
    @classmethod
    def sanitize_phone(cls, v: str) -> str:
        """Strip newlines, null bytes, ANSI escapes, and whitespace from phone input."""
        stripped_ansi = _ANSI_ESCAPE_RE.sub("", v)
        clean = (
            stripped_ansi.replace("\n", "")
            .replace("\r", "")
            .replace("\0", "")
            .replace(" ", "")
            .strip()
        )
        if not clean:
            raise ValueError("Phone number cannot be empty")
        return clean


class DNCRequestInput(BaseModel):
    """Validated input for the record_dnc_request statutory tool."""

    model_config = ConfigDict(extra="forbid")

    phone: str = Field(min_length=7, max_length=25)
    reason: str = Field(
        default="Caller requested removal",
        max_length=200,
    )

    @field_validator("phone")
    @classmethod
    def sanitize_phone(cls, v: str) -> str:
        """Strip raw formatting characters from phone string."""
        stripped_ansi = _ANSI_ESCAPE_RE.sub("", v)
        clean = (
            stripped_ansi.replace("\n", "")
            .replace("\r", "")
            .replace("\0", "")
            .replace(" ", "")
            .strip()
        )
        if not clean:
            raise ValueError("Phone number cannot be empty")
        return clean

    @field_validator("reason")
    @classmethod
    def sanitize_reason(cls, v: str) -> str:
        """Strip newlines and ANSI escapes from DNC reason."""
        stripped_ansi = _ANSI_ESCAPE_RE.sub("", v)
        return (
            stripped_ansi.replace("\n", " ")
            .replace("\r", " ")
            .replace("\0", "")
            .strip()
        )


class TransferCallInput(BaseModel):
    """Validated input for the transfer_call escalation tool."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        default="Customer requested human agent",
        max_length=200,
    )
    department: str = Field(default="front_desk", max_length=50)

    @field_validator("reason")
    @classmethod
    def sanitize_reason(cls, v: str) -> str:
        """Strip newlines and ANSI escapes from transfer reason."""
        stripped_ansi = _ANSI_ESCAPE_RE.sub("", v)
        return (
            stripped_ansi.replace("\n", " ")
            .replace("\r", " ")
            .replace("\0", "")
            .strip()
        )


class TimeSlot(BaseModel):
    """Discrete appointment slot interval aligned to clinic business hours."""

    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    is_available: bool = True
