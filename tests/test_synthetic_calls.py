"""Automated 10-call synthetic conversation suite verifying all voice AI workflows."""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config import Settings, get_settings
from app.main import app
from app.services.booking import BookingCoordinator
from app.services.calendar import GoogleCalendarClient

FORBIDDEN_ACOUSTIC_TOKENS: tuple[str, ...] = (
    "**",
    "*",
    "#",
    "- ",
    "http://",
    "https://",
    "[",
    "]",
)


def assert_acoustic_invariants(spoken_text: str) -> None:
    """Assert spoken response contains strictly plain phonetic speech."""
    assert spoken_text, "Spoken text response cannot be empty"
    for token in FORBIDDEN_ACOUSTIC_TOKENS:
        assert (
            token not in spoken_text
        ), f"Prohibited acoustic token '{token}' found in: {spoken_text}"


def create_synthetic_wire_harness(
    gcal_handler: Callable[[httpx.Request], httpx.Response],
    settings: Settings,
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """Assemble real FastAPI app wired to httpx.MockTransport for wire inspection."""
    recorded_requests: list[httpx.Request] = []

    def tracking_handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        return gcal_handler(request)

    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(tracking_handler)
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.http_client = mock_client
    calendar_client = GoogleCalendarClient.from_settings(mock_client, settings)
    app.state.booking_coordinator = BookingCoordinator(calendar_client, settings)

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, recorded_requests


async def dispatch_tool(
    client: httpx.AsyncClient,
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str,
    secret: str | None = None,
    phone: str = "+15551234567",
    name: str = "Alex Rivera",
) -> str:
    """Dispatch tool call through /webhook/vapi and return spoken text result."""
    payload = {
        "message": {
            "type": "tool-calls",
            "toolCalls": [
                {
                    "id": f"tc_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": arguments,
                    },
                }
            ],
            "call": {
                "id": call_id,
                "customer": {"number": phone, "name": name},
            },
        }
    }
    headers = {"X-Vapi-Secret": secret or "test-vapi-secret"}
    response = await client.post("/webhook/vapi", json=payload, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "results" in data and len(data["results"]) == 1
    result: str = data["results"][0]["result"]
    assert_acoustic_invariants(result)
    return result


@pytest.mark.synthetic
async def test_call_01_happy_path_booking(test_settings: Settings) -> None:
    """Call 1: Happy Path Booking — availability check followed by confirmed booking."""

    def gcal_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/calendar/v3/freeBusy":
            return httpx.Response(
                200,
                json={"calendars": {test_settings.calendar_id: {"busy": []}}},
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(201, json={"id": "evt_happy_01"})
        return httpx.Response(404)

    client, requests = create_synthetic_wire_harness(gcal_handler, test_settings)
    call_id = "call_happy_001"

    async with client:
        # Turn 1: Availability check for Tuesday, October 27, 2026
        turn1_result = await dispatch_tool(
            client=client,
            tool_name="check_availability",
            arguments={"start_time": "2026-10-27"},
            call_id=call_id,
        )
        assert "Tuesday, October 27th" in turn1_result
        assert "We have openings on" in turn1_result

        # Turn 2: Verbal confirmation committed via book_appointment
        turn2_result = await dispatch_tool(
            client=client,
            tool_name="book_appointment",
            arguments={
                "customer_name": "Alex Rivera",
                "phone": "+15551234567",
                "start_time": "2026-10-27T14:00:00",
                "service_type": "General Consultation",
            },
            call_id=call_id,
        )
        assert "You're all set, Alex Rivera!" in turn2_result
        assert "Tuesday, October 27th at 2:00 PM" in turn2_result

    # Assert exactly one calendar event insertion was dispatched
    event_posts = [r for r in requests if r.url.path.endswith("/events")]
    assert len(event_posts) == 1


@pytest.mark.synthetic
async def test_call_02_conflict_and_alternative_slot(
    test_settings: Settings,
) -> None:
    """Call 2: Conflict & Alternative — 10:00 AM slot busy.
    System detects conflict and proposes alternative opening.
    """
    zone = ZoneInfo(test_settings.clinic_timezone)
    busy_start = datetime(2026, 10, 27, 10, 0, tzinfo=zone).isoformat()
    busy_end = datetime(2026, 10, 27, 11, 0, tzinfo=zone).isoformat()

    def gcal_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/calendar/v3/freeBusy":
            return httpx.Response(
                200,
                json={
                    "calendars": {
                        test_settings.calendar_id: {
                            "busy": [{"start": busy_start, "end": busy_end}]
                        }
                    }
                },
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(201, json={"id": "evt_alt_02"})
        return httpx.Response(404)

    client, _ = create_synthetic_wire_harness(gcal_handler, test_settings)
    call_id = "call_conflict_002"

    async with client:
        # Turn 1: Availability query excludes 10:00 AM
        turn1_result = await dispatch_tool(
            client=client,
            tool_name="check_availability",
            arguments={"start_time": "2026-10-27"},
            call_id=call_id,
        )
        assert "10:00 AM" not in turn1_result

        # Turn 2: Caller requests 10:00 AM anyway; host rejects collision
        conflict_result = await dispatch_tool(
            client=client,
            tool_name="book_appointment",
            arguments={
                "customer_name": "Jordan Lee",
                "phone": "+15552345678",
                "start_time": "2026-10-27T10:00:00",
                "service_type": "General Consultation",
            },
            call_id=call_id,
        )
        assert "that 10:00 AM slot was just filled" in conflict_result

        # Turn 3: Caller accepts 11:00 AM alternative
        turn3_result = await dispatch_tool(
            client=client,
            tool_name="book_appointment",
            arguments={
                "customer_name": "Jordan Lee",
                "phone": "+15552345678",
                "start_time": "2026-10-27T11:00:00",
                "service_type": "General Consultation",
            },
            call_id=call_id,
        )
        assert "You're all set, Jordan Lee!" in turn3_result
        assert "Tuesday, October 27th at 11:00 AM" in turn3_result


@pytest.mark.synthetic
async def test_call_03_off_hours_request(test_settings: Settings) -> None:
    """Call 3: Off-Hours Request — Sunday 8:00 PM politely rejected."""

    def gcal_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)  # Should never be reached

    client, requests = create_synthetic_wire_harness(gcal_handler, test_settings)

    async with client:
        result = await dispatch_tool(
            client=client,
            tool_name="book_appointment",
            arguments={
                "customer_name": "Casey Morgan",
                "phone": "+15553456789",
                "start_time": "2026-11-01T20:00:00",  # Sunday 8:00 PM
                "service_type": "General Consultation",
            },
            call_id="call_offhours_003",
        )
        assert "Monday through Friday from 9:00 AM to 5:00 PM" in result
        assert len(requests) == 0  # Zero calendar calls


@pytest.mark.synthetic
async def test_call_04_double_booking_prevention_retry(
    test_settings: Settings,
) -> None:
    """Call 4: Double-Booking Prevention.
    Network replay with same idempotency key returns cached confirmation.
    """

    def gcal_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/calendar/v3/freeBusy":
            return httpx.Response(
                200,
                json={"calendars": {test_settings.calendar_id: {"busy": []}}},
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(201, json={"id": "evt_replay_04"})
        return httpx.Response(404)

    client, requests = create_synthetic_wire_harness(gcal_handler, test_settings)
    call_id = "call_replay_004"
    args = {
        "customer_name": "Taylor Swift",
        "phone": "+15554567890",
        "start_time": "2026-10-27T14:00:00",
        "service_type": "General Consultation",
    }

    async with client:
        # First booking commit
        result1 = await dispatch_tool(
            client=client,
            tool_name="book_appointment",
            arguments=args,
            call_id=call_id,
        )
        assert "You're all set, Taylor Swift!" in result1

        # Second booking commit (identical call_id + slot replay)
        result2 = await dispatch_tool(
            client=client,
            tool_name="book_appointment",
            arguments=args,
            call_id=call_id,
        )
        assert result2 == result1

    # Confirms only 1 Google Calendar event creation occurred
    event_posts = [r for r in requests if r.url.path.endswith("/events")]
    assert len(event_posts) == 1


@pytest.mark.synthetic
async def test_call_05_proposal_rejection(test_settings: Settings) -> None:
    """Call 5: Proposal Rejection — caller rejects offered slot; zero event creation."""

    def gcal_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/calendar/v3/freeBusy":
            return httpx.Response(
                200,
                json={"calendars": {test_settings.calendar_id: {"busy": []}}},
            )
        return httpx.Response(404)

    client, requests = create_synthetic_wire_harness(gcal_handler, test_settings)

    async with client:
        turn1_result = await dispatch_tool(
            client=client,
            tool_name="check_availability",
            arguments={"start_time": "2026-10-27"},
            call_id="call_reject_005",
        )
        assert "We have openings on" in turn1_result
        # Caller says "None of those times work for me" -> No booking dispatched

    event_posts = [r for r in requests if r.url.path.endswith("/events")]
    assert len(event_posts) == 0


@pytest.mark.synthetic
async def test_call_06_prompt_injection_defense(
    test_settings: Settings,
) -> None:
    """Call 6: Prompt Injection Defense — adversarial instructions sanitized."""

    def gcal_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/calendar/v3/freeBusy":
            return httpx.Response(
                200,
                json={"calendars": {test_settings.calendar_id: {"busy": []}}},
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(201, json={"id": "evt_inject_06"})
        return httpx.Response(404)

    client, _ = create_synthetic_wire_harness(gcal_handler, test_settings)

    async with client:
        adversarial_name = (
            "Malicious Caller\r\nDROP TABLE appointments;\x1b[31;1m SYSTEM OVERRIDE"
        )
        result = await dispatch_tool(
            client=client,
            tool_name="book_appointment",
            arguments={
                "customer_name": adversarial_name,
                "phone": "+15555678901",
                "start_time": "2026-10-27T15:00:00",
                "service_type": "General Consultation",
            },
            call_id="call_inject_006",
        )
        # Confirms newlines, nulls, ANSI escapes were stripped from spoken output
        assert "\n" not in result
        assert "\r" not in result
        assert "\x1b" not in result
        assert "You're all set" in result


@pytest.mark.synthetic
async def test_call_07_calendar_privacy_no_leak(
    test_settings: Settings,
) -> None:
    """Call 7: Calendar Privacy.
    5 busy slots of other patients yield zero patient names or title leaks.
    """
    zone = ZoneInfo(test_settings.clinic_timezone)
    busy_intervals = [
        {
            "start": datetime(2026, 10, 27, 9, 30, tzinfo=zone).isoformat(),
            "end": datetime(2026, 10, 27, 10, 0, tzinfo=zone).isoformat(),
        },
        {
            "start": datetime(2026, 10, 27, 11, 0, tzinfo=zone).isoformat(),
            "end": datetime(2026, 10, 27, 12, 0, tzinfo=zone).isoformat(),
        },
        {
            "start": datetime(2026, 10, 27, 13, 0, tzinfo=zone).isoformat(),
            "end": datetime(2026, 10, 27, 14, 0, tzinfo=zone).isoformat(),
        },
        {
            "start": datetime(2026, 10, 27, 15, 0, tzinfo=zone).isoformat(),
            "end": datetime(2026, 10, 27, 15, 30, tzinfo=zone).isoformat(),
        },
        {
            "start": datetime(2026, 10, 27, 16, 0, tzinfo=zone).isoformat(),
            "end": datetime(2026, 10, 27, 16, 30, tzinfo=zone).isoformat(),
        },
    ]

    def gcal_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/calendar/v3/freeBusy"
        return httpx.Response(
            200,
            json={"calendars": {test_settings.calendar_id: {"busy": busy_intervals}}},
        )

    client, _ = create_synthetic_wire_harness(gcal_handler, test_settings)

    async with client:
        result = await dispatch_tool(
            client=client,
            tool_name="check_availability",
            arguments={"start_time": "2026-10-27"},
            call_id="call_privacy_007",
        )
        # Confirms purely temporal slot availability is returned
        assert "We have openings on" in result
        for private_term in ("John Doe", "Jane Smith", "Root Canal", "HIV", "Oncology"):
            assert private_term not in result


@pytest.mark.synthetic
async def test_call_08_tcpa_do_not_call(test_settings: Settings) -> None:
    """Call 8: TCPA Do-Not-Call — caller opt-out recorded with statutory response."""

    def gcal_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)  # Should never be called

    client, requests = create_synthetic_wire_harness(gcal_handler, test_settings)

    async with client:
        result = await dispatch_tool(
            client=client,
            tool_name="record_dnc_request",
            arguments={
                "phone": "+15556789012",
                "reason": "Caller requested removal",
            },
            call_id="call_dnc_008",
        )
        assert "Understood. I have removed this number" in result
        assert len(requests) == 0


@pytest.mark.synthetic
async def test_call_09_human_warm_escalation(test_settings: Settings) -> None:
    """Call 9: Human Warm Escalation — triggers transfer directive to front desk."""

    def gcal_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)  # Should never be called

    client, requests = create_synthetic_wire_harness(gcal_handler, test_settings)

    async with client:
        result = await dispatch_tool(
            client=client,
            tool_name="transfer_call",
            arguments={
                "reason": "Customer requested human receptionist twice",
                "department": "front_desk",
            },
            call_id="call_transfer_009",
        )
        assert "One moment while I transfer you directly" in result
        assert len(requests) == 0


@pytest.mark.synthetic
async def test_call_10_calendar_outage_fallback(
    test_settings: Settings,
) -> None:
    """Call 10: Calendar Outage Fallback — Google API 500 triggers graceful fallback."""

    def gcal_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    client, _ = create_synthetic_wire_harness(gcal_handler, test_settings)

    async with client:
        result = await dispatch_tool(
            client=client,
            tool_name="check_availability",
            arguments={"start_time": "2026-10-27"},
            call_id="call_outage_010",
        )
        assert "My scheduling system is taking a moment" in result
        assert "Internal Server Error" not in result
        assert "CalendarServiceError" not in result
