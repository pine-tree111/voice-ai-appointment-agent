"""Unit tests for configuration, security sanitizers, and models."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.errors import (
    CalendarServiceError,
    DNCRequestedError,
    DoubleBookingError,
    EscalationRequiredError,
    InvalidTimezoneError,
    SlotUnavailableError,
    SSRFError,
    TimeRangeError,
    VoiceAgentError,
    WebhookAuthError,
)
from app.logging import (
    configure_logging,
    get_logger,
    sanitize_log_injection,
    sanitize_log_string,
)
from app.models.appointment import (
    BookAppointmentInput,
    CheckAvailabilityInput,
    DNCRequestInput,
    TimeSlot,
    TransferCallInput,
)
from app.models.vapi import (
    ToolName,
    VapiFunctionCall,
    VapiToolResultItem,
    VapiWebhookPayload,
    VapiWebhookResponse,
)

# ============================================================================
# Settings & Configuration Tests
# ============================================================================


def test_settings_defaults() -> None:
    """Verify application settings defaults conform to business requirements."""
    settings = Settings()
    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.clinic_name == "Acme Clinic"
    assert settings.clinic_timezone == "America/New_York"
    assert settings.business_hours_start == 9
    assert settings.business_hours_end == 17
    assert settings.default_slot_duration_minutes == 30
    assert settings.max_query_days == 14
    assert settings.webhook_timeout_seconds == 2.5


def test_secret_str_masking() -> None:
    """Verify SecretStr fields mask credentials from repr, str, and logs."""
    settings = Settings()
    assert settings.vapi_secret_token.get_secret_value() == "dev-vapi-secret"
    assert "dev-vapi-secret" not in str(settings.vapi_secret_token)
    assert "dev-vapi-secret" not in repr(settings.vapi_secret_token)
    assert "mock-token" not in repr(settings.google_calendar_token)


@pytest.mark.parametrize(
    "blocked_host",
    [
        "169.254.169.254",
        "metadata.google.internal",
        "metadata.internal",
        "instance-data",
    ],
)
def test_calendar_url_ssrf_blocked_hosts(blocked_host: str) -> None:
    """Verify SSRF validator blocks cloud metadata endpoints."""
    with pytest.raises(SSRFError, match="SSRF attempt blocked"):
        Settings(google_calendar_base_url=f"https://{blocked_host}/calendar/v3")


@pytest.mark.parametrize("scheme", ["ftp", "gopher", "file", "ldap"])
def test_calendar_url_invalid_scheme(scheme: str) -> None:
    """Verify SSRF validator rejects non-HTTP(S) schemes."""
    with pytest.raises(SSRFError, match="Invalid calendar URL scheme"):
        Settings(google_calendar_base_url=f"{scheme}://calendar.googleapis.com/v3")


def test_calendar_url_valid_https() -> None:
    """Verify valid external calendar URLs pass validation."""
    valid_url = "https://www.googleapis.com/calendar/v3"
    settings = Settings(google_calendar_base_url=valid_url)
    assert settings.google_calendar_base_url == valid_url


def test_get_settings_singleton() -> None:
    """Verify get_settings provides a cached lazy singleton."""
    first = get_settings()
    second = get_settings()
    assert first is second


# ============================================================================
# Logging & Security Sanitization Tests
# ============================================================================


def test_sanitize_log_string() -> None:
    """Verify log sanitizer strips raw newlines, null bytes, and ANSI escapes."""
    raw = "inbound\0data\r\nwith \x1b[31mred ANSI\x1b[0m"
    sanitized = sanitize_log_string(raw)
    assert "\0" not in sanitized
    assert "\r" not in sanitized
    assert "\n" not in sanitized
    assert "\x1b" not in sanitized
    assert sanitized == "inbounddata  with red ANSI"


def test_sanitize_log_string_truncation() -> None:
    """Verify log sanitizer bounds input lengths."""
    huge_input = "a" * 15_000
    sanitized = sanitize_log_string(huge_input, max_length=100)
    assert len(sanitized) == 100


def test_sanitize_log_injection_processor() -> None:
    """Verify structlog processor cleans nested event structures."""
    event_dict: dict[str, Any] = {
        "event": "caller\nquery\0\x1b[31mattack\x1b[0m",
        "nested": {"caller": "Alex\r\nRivera"},
        "items": ["safe", "unsafe\nstring"],
        "num": 42,
    }
    cleaned = sanitize_log_injection(None, "info", event_dict)
    assert cleaned["event"] == "caller queryattack"
    assert cleaned["nested"] == {"caller": "Alex  Rivera"}
    assert cleaned["items"] == ["safe", "unsafe string"]
    assert cleaned["num"] == 42


def test_configure_logging() -> None:
    """Verify configure_logging initializes logger cleanly."""
    configure_logging()
    logger = get_logger("test_suite")
    assert logger is not None


# ============================================================================
# Domain Errors Tests
# ============================================================================


def test_domain_error_hierarchy() -> None:
    """Verify all custom domain exceptions inherit from VoiceAgentError."""
    assert issubclass(WebhookAuthError, VoiceAgentError)
    assert issubclass(SSRFError, VoiceAgentError)
    assert issubclass(SlotUnavailableError, VoiceAgentError)
    assert issubclass(DoubleBookingError, VoiceAgentError)
    assert issubclass(CalendarServiceError, VoiceAgentError)
    assert issubclass(DNCRequestedError, VoiceAgentError)
    assert issubclass(EscalationRequiredError, VoiceAgentError)
    assert issubclass(InvalidTimezoneError, VoiceAgentError)
    assert issubclass(TimeRangeError, VoiceAgentError)


# ============================================================================
# Telephony Model Tests (Vapi)
# ============================================================================


def test_vapi_function_call_valid_dict() -> None:
    """Verify VapiFunctionCall parses native dictionary arguments."""
    func = VapiFunctionCall(
        name=ToolName.CHECK_AVAILABILITY,
        arguments={"start_time": "2026-10-28"},
    )
    assert func.name == ToolName.CHECK_AVAILABILITY
    assert func.arguments["start_time"] == "2026-10-28"


def test_vapi_function_call_valid_json_string() -> None:
    """Verify VapiFunctionCall deserializes JSON string arguments."""
    func = VapiFunctionCall.model_validate(
        {
            "name": "check_availability",
            "arguments": '{"start_time": "2026-10-28"}',
        }
    )
    assert func.name == ToolName.CHECK_AVAILABILITY
    assert func.arguments["start_time"] == "2026-10-28"


def test_vapi_function_call_malformed_json_fails() -> None:
    """Verify VapiFunctionCall raises ValidationError on invalid JSON string."""
    with pytest.raises(ValidationError, match="Malformed JSON in tool arguments"):
        VapiFunctionCall.model_validate(
            {"name": "check_availability", "arguments": "{invalid-json}"}
        )


def test_vapi_function_call_unmapped_tool_fails_closed() -> None:
    """Verify unmapped tool name fails closed with ValidationError."""
    with pytest.raises(ValidationError):
        VapiFunctionCall.model_validate({"name": "delete_all_events", "arguments": {}})


def test_vapi_webhook_payload_tool_calls_alias() -> None:
    """Verify VapiWebhookPayload handles both toolCalls and toolCallList aliases."""
    payload_data = {
        "message": {
            "type": "tool-calls",
            "toolCallList": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {
                        "name": "check_availability",
                        "arguments": {"start_time": "2026-10-28"},
                    },
                }
            ],
            "call": {
                "id": "call_123",
                "customer": {"number": "+15551234567", "name": "Alex"},
            },
        }
    }
    payload = VapiWebhookPayload.model_validate(payload_data)
    assert len(payload.message.tool_calls) == 1
    assert payload.message.tool_calls[0].id == "tool_1"
    assert payload.message.call is not None
    assert payload.message.call.customer.name == "Alex"


def test_vapi_tool_result_item_serialization() -> None:
    """Verify VapiToolResultItem serializes to toolCallId wire format."""
    item = VapiToolResultItem(
        tool_call_id="call_abc",
        result="Tuesday at 2:00 PM is open.",
    )
    dumped = item.model_dump(by_alias=True)
    assert dumped["toolCallId"] == "call_abc"
    assert dumped["result"] == "Tuesday at 2:00 PM is open."


def test_vapi_webhook_response() -> None:
    """Verify VapiWebhookResponse encapsulates a list of result items."""
    resp = VapiWebhookResponse(
        results=[VapiToolResultItem(tool_call_id="1", result="Appointment confirmed.")]
    )
    assert len(resp.results) == 1
    assert resp.results[0].tool_call_id == "1"


# ============================================================================
# Appointment Model Tests
# ============================================================================


def test_check_availability_input_aliases() -> None:
    """Verify CheckAvailabilityInput parses start_date and end_date aliases."""
    model = CheckAvailabilityInput.model_validate(
        {"start_date": "2026-10-28", "end_date": "2026-10-29"}
    )
    assert model.start_time == "2026-10-28"
    assert model.end_time == "2026-10-29"
    assert model.timezone == "America/New_York"


def test_book_appointment_sanitization() -> None:
    """Verify BookAppointmentInput sanitizes caller name and phone number."""
    model = BookAppointmentInput.model_validate(
        {
            "customer_name": "Dr. Alex \n Rivera\0",
            "phone": "+1 (555) 123-4567\r\n",
            "start_time": "2026-10-28T14:00:00",
        }
    )
    assert model.customer_name == "Dr. Alex   Rivera"
    assert model.phone == "+1(555)123-4567"
    assert model.service_type == "General Consultation"
    assert model.idempotency_key is None


def test_book_appointment_empty_fields_fail() -> None:
    """Verify empty or whitespace-only name and phone raise ValidationError."""
    with pytest.raises(ValidationError):
        BookAppointmentInput.model_validate(
            {
                "customer_name": "  \n\0  ",
                "phone": "+15551234567",
                "start_time": "2026-10-28T14:00:00",
            }
        )

    with pytest.raises(ValidationError):
        BookAppointmentInput.model_validate(
            {
                "customer_name": "Alex Rivera",
                "phone": "   \n   ",
                "start_time": "2026-10-28T14:00:00",
            }
        )


def test_dnc_request_input_sanitization() -> None:
    """Verify DNCRequestInput cleans phone formatting."""
    dnc = DNCRequestInput.model_validate(
        {"phone": "+1 (555) 999-0000\n", "reason": "Opt out"}
    )
    assert dnc.phone == "+1(555)999-0000"
    assert dnc.reason == "Opt out"


def test_transfer_call_input_defaults() -> None:
    """Verify TransferCallInput defaults to front_desk department."""
    transfer = TransferCallInput.model_validate(
        {"reason": "Caller requested human supervisor"}
    )
    assert transfer.department == "front_desk"
    assert transfer.reason == "Caller requested human supervisor"


def test_timeslot_model() -> None:
    """Verify TimeSlot holds timezone-aware datetimes and availability flag."""
    start = datetime(2026, 10, 28, 14, 0, tzinfo=UTC)
    end = datetime(2026, 10, 28, 14, 30, tzinfo=UTC)
    slot = TimeSlot(start=start, end=end, is_available=True)
    assert slot.start == start
    assert slot.end == end
    assert slot.is_available is True
