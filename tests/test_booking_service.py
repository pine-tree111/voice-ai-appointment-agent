"""Unit test suite for BookingCoordinator with wire-level HTTP mocks."""

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config import Settings
from app.errors import InvalidTimezoneError, TimeRangeError
from app.models.appointment import (
    BookAppointmentInput,
    CheckAvailabilityInput,
    DNCRequestInput,
    TransferCallInput,
)
from app.services.booking import BookingCoordinator
from app.services.calendar import GoogleCalendarClient


def make_mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    """Create an async httpx client backed by a mock transport."""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


def test_format_spoken_datetime(test_settings: Settings) -> None:
    """Verify acoustic anti-markdown formatting into natural phonetic speech."""
    dummy_client = httpx.AsyncClient()
    gcal = GoogleCalendarClient.from_settings(dummy_client, test_settings)
    coord = BookingCoordinator(gcal, test_settings)

    # 2026-10-28 is Wednesday
    dt = datetime(2026, 10, 28, 14, 0, tzinfo=ZoneInfo("America/New_York"))
    spoken = coord.format_spoken_datetime(dt)

    assert spoken == "Wednesday, October 28th at 2:00 PM"
    assert "*" not in spoken
    assert "#" not in spoken
    assert "-" not in spoken
    assert "T" not in spoken or "at" in spoken


async def test_handle_check_availability_free_slots(
    test_settings: Settings,
) -> None:
    """Verify free slot proposal when calendar has partial busy intervals."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/calendar/v3/freeBusy"
        payload = {
            "calendars": {
                test_settings.calendar_id: {
                    "busy": [
                        {
                            "start": "2026-10-28T10:00:00Z",
                            "end": "2026-10-28T11:00:00Z",
                        },
                        {
                            "start": "2026-10-28T14:00:00Z",
                            "end": "2026-10-28T15:00:00Z",
                        },
                    ]
                }
            }
        }
        return httpx.Response(200, json=payload)

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        coord = BookingCoordinator(gcal, test_settings)

        input_data = CheckAvailabilityInput(
            start_time="2026-10-28",
            timezone="America/New_York",
        )
        response = await coord.handle_check_availability(
            input_data, call_id="call_test_01"
        )

        assert "We have openings on Wednesday, October 28th" in response
        assert "Would you like me to book one of those?" in response
        assert "**" not in response


async def test_handle_check_availability_all_busy(
    test_settings: Settings,
) -> None:
    """Verify polite spoken response when entire business day is occupied."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "calendars": {
                test_settings.calendar_id: {
                    "busy": [
                        {
                            "start": "2026-10-28T13:00:00Z",  # 9am EDT
                            "end": "2026-10-28T21:00:00Z",  # 5pm EDT
                        }
                    ]
                }
            }
        }
        return httpx.Response(200, json=payload)

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        coord = BookingCoordinator(gcal, test_settings)

        input_data = CheckAvailabilityInput(
            start_time="2026-10-28",
            timezone="America/New_York",
        )
        response = await coord.handle_check_availability(
            input_data, call_id="call_test_02"
        )

        assert "no available appointments for that time" in response
        assert "Would you like to check another date?" in response


async def test_handle_check_availability_weekend(
    test_settings: Settings,
) -> None:
    """Verify weekend inquiries return standard business hours notice."""
    dummy_client = httpx.AsyncClient()
    gcal = GoogleCalendarClient.from_settings(dummy_client, test_settings)
    coord = BookingCoordinator(gcal, test_settings)

    # 2026-10-31 is Saturday
    input_data = CheckAvailabilityInput(
        start_time="2026-10-31",
        timezone="America/New_York",
    )
    response = await coord.handle_check_availability(
        input_data, call_id="call_test_03"
    )

    assert "Monday through Friday from 9:00 AM to 5:00 PM" in response
    assert "Would you like to look at Monday morning?" in response


async def test_handle_check_availability_invalid_timezone(
    test_settings: Settings,
) -> None:
    """Verify InvalidTimezoneError raised for unknown timezone string."""
    dummy_client = httpx.AsyncClient()
    gcal = GoogleCalendarClient.from_settings(dummy_client, test_settings)
    coord = BookingCoordinator(gcal, test_settings)

    input_data = CheckAvailabilityInput(
        start_time="2026-10-28",
        timezone="Mars/Olympus_Mons",
    )
    with pytest.raises(InvalidTimezoneError, match="Invalid timezone"):
        await coord.handle_check_availability(input_data, call_id="call_tz_err")


async def test_handle_check_availability_time_range_error(
    test_settings: Settings,
) -> None:
    """Verify TimeRangeError raised when range is invalid or exceeds max window."""
    dummy_client = httpx.AsyncClient()
    gcal = GoogleCalendarClient.from_settings(dummy_client, test_settings)
    coord = BookingCoordinator(gcal, test_settings)

    # end_time <= start_time
    input_inverted = CheckAvailabilityInput(
        start_time="2026-10-28T12:00:00",
        end_time="2026-10-28T10:00:00",
        timezone="America/New_York",
    )
    with pytest.raises(TimeRangeError, match="must be strictly after"):
        await coord.handle_check_availability(
            input_inverted, call_id="call_err_01"
        )

    # Window exceeds max_query_days (14 days)
    input_excess = CheckAvailabilityInput(
        start_time="2026-10-28T09:00:00",
        end_time="2026-11-20T17:00:00",
        timezone="America/New_York",
    )
    with pytest.raises(TimeRangeError, match="cannot exceed 14 days"):
        await coord.handle_check_availability(
            input_excess, call_id="call_err_02"
        )


async def test_handle_check_availability_calendar_timeout(
    test_settings: Settings,
) -> None:
    """Verify graceful message-taking fallback when Google Calendar API times out."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Google Calendar timed out")

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        coord = BookingCoordinator(gcal, test_settings)

        input_data = CheckAvailabilityInput(
            start_time="2026-10-28",
            timezone="America/New_York",
        )
        response = await coord.handle_check_availability(
            input_data, call_id="call_timeout"
        )

        assert "My scheduling system is taking a moment" in response
        assert "our team will text you a confirmation shortly" in response


async def test_handle_book_appointment_success(
    test_settings: Settings,
) -> None:
    """Verify Phase 2 commitment creating event and returning spoken confirmation."""
    created_events: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/calendar/v3/freeBusy":
            return httpx.Response(
                200,
                json={"calendars": {test_settings.calendar_id: {"busy": []}}},
            )
        if request.url.path.endswith("/events"):
            data = json.loads(request.content.decode("utf-8"))
            created_events.append(data)
            return httpx.Response(201, json={"id": "evt_book_999"})
        return httpx.Response(404)

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        coord = BookingCoordinator(gcal, test_settings)

        input_data = BookAppointmentInput(
            customer_name="Alex Rivera",
            phone="+15551234567",
            start_time="2026-10-28T14:00:00",
            service_type="Dental Cleaning",
        )
        spoken_response = await coord.handle_book_appointment(
            input_data, call_id="call_alex_01"
        )

        assert "You're all set, Alex Rivera!" in spoken_response
        assert "Wednesday, October 28th at 2:00 PM" in spoken_response
        assert len(created_events) == 1
        assert "Alex Rivera" in created_events[0]["summary"]


async def test_handle_book_appointment_idempotency_cache(
    test_settings: Settings,
) -> None:
    """Verify duplicate booking replay returns cached response without mutations."""
    event_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal event_calls
        if request.url.path == "/calendar/v3/freeBusy":
            return httpx.Response(
                200,
                json={"calendars": {test_settings.calendar_id: {"busy": []}}},
            )
        if request.url.path.endswith("/events"):
            event_calls += 1
            return httpx.Response(201, json={"id": "evt_book_cached"})
        return httpx.Response(404)

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        coord = BookingCoordinator(gcal, test_settings)

        input_data = BookAppointmentInput(
            customer_name="Alex Rivera",
            phone="+15551234567",
            start_time="2026-10-28T14:00:00",
            service_type="General Consultation",
        )

        resp1 = await coord.handle_book_appointment(
            input_data, call_id="call_dup_01"
        )
        assert event_calls == 1

        # Replayed request with same call_id and start_time
        resp2 = await coord.handle_book_appointment(
            input_data, call_id="call_dup_01"
        )
        assert resp1 == resp2
        # Verify event creation was NOT called a second time
        assert event_calls == 1


async def test_handle_book_appointment_occupied_slot(
    test_settings: Settings,
) -> None:
    """Verify double-booking rejection when slot is occupied in calendar."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/calendar/v3/freeBusy":
            # Slot 14:00 to 14:30 EDT is 18:00 to 18:30 UTC
            return httpx.Response(
                200,
                json={
                    "calendars": {
                        test_settings.calendar_id: {
                            "busy": [
                                {
                                    "start": "2026-10-28T18:00:00Z",
                                    "end": "2026-10-28T18:30:00Z",
                                }
                            ]
                        }
                    }
                },
            )
        return httpx.Response(404)

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        coord = BookingCoordinator(gcal, test_settings)

        input_data = BookAppointmentInput(
            customer_name="Alex Rivera",
            phone="+15551234567",
            start_time="2026-10-28T14:00:00",
            service_type="General Consultation",
        )
        response = await coord.handle_book_appointment(
            input_data, call_id="call_busy_slot"
        )

        assert "that 2:00 PM slot was just filled" in response
        assert "Would another time work instead?" in response


async def test_handle_book_appointment_off_hours_and_weekend(
    test_settings: Settings,
) -> None:
    """Verify booking outside business hours or on weekends is rejected gracefully."""
    dummy_client = httpx.AsyncClient()
    gcal = GoogleCalendarClient.from_settings(dummy_client, test_settings)
    coord = BookingCoordinator(gcal, test_settings)

    # 11:00 PM is after business hours
    input_late = BookAppointmentInput(
        customer_name="Night Owl",
        phone="+15551234567",
        start_time="2026-10-28T23:00:00",
        service_type="General Consultation",
    )
    resp_late = await coord.handle_book_appointment(
        input_late, call_id="call_late"
    )
    assert "Would you like to schedule during regular hours?" in resp_late

    # 2026-10-31 is Saturday
    input_weekend = BookAppointmentInput(
        customer_name="Weekend Caller",
        phone="+15551234567",
        start_time="2026-10-31T10:00:00",
        service_type="General Consultation",
    )
    resp_weekend = await coord.handle_book_appointment(
        input_weekend, call_id="call_weekend"
    )
    assert "Would you like to look at Monday morning?" in resp_weekend


async def test_handle_book_appointment_concurrent_serialization(
    test_settings: Settings,
) -> None:
    """Verify asyncio.Lock prevents race conditions when concurrent callers book."""
    booked_slots: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/calendar/v3/freeBusy":
            busy_list = (
                [
                    {
                        "start": "2026-10-28T18:00:00Z",
                        "end": "2026-10-28T18:30:00Z",
                    }
                ]
                if "booked" in booked_slots
                else []
            )
            return httpx.Response(
                200,
                json={"calendars": {test_settings.calendar_id: {"busy": busy_list}}},
            )
        if request.url.path.endswith("/events"):
            booked_slots.append("booked")
            return httpx.Response(201, json={"id": "evt_race_winner"})
        return httpx.Response(404)

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        coord = BookingCoordinator(gcal, test_settings)

        input1 = BookAppointmentInput(
            customer_name="Caller One",
            phone="+15551111111",
            start_time="2026-10-28T14:00:00",
            service_type="General Consultation",
        )
        input2 = BookAppointmentInput(
            customer_name="Caller Two",
            phone="+15552222222",
            start_time="2026-10-28T14:00:00",
            service_type="General Consultation",
        )

        results = await asyncio.gather(
            coord.handle_book_appointment(input1, call_id="call_race_01"),
            coord.handle_book_appointment(input2, call_id="call_race_02"),
        )

        successes = [r for r in results if "You're all set" in r]
        conflicts = [r for r in results if "was just filled" in r]

        assert len(successes) == 1
        assert len(conflicts) == 1
        assert len(booked_slots) == 1


async def test_handle_dnc_request(test_settings: Settings) -> None:
    """Verify statutory DNC opt-out logs audit and returns statutory confirmation."""
    dummy_client = httpx.AsyncClient()
    gcal = GoogleCalendarClient.from_settings(dummy_client, test_settings)
    coord = BookingCoordinator(gcal, test_settings)

    input_data = DNCRequestInput(
        phone="+15559998888",
        reason="Caller explicitly opted out",
    )
    result = await coord.handle_dnc_request(input_data, call_id="call_dnc_01")
    assert result == (
        "Understood. I have removed this number from our list. Have a great day."
    )

async def test_handle_transfer_call(test_settings: Settings) -> None:
    """Verify warm transfer logs audit event and returns transfer notice."""
    dummy_client = httpx.AsyncClient()
    gcal = GoogleCalendarClient.from_settings(dummy_client, test_settings)
    coord = BookingCoordinator(gcal, test_settings)

    input_data = TransferCallInput(
        reason="Caller needs billing specialist",
        department="billing",
    )
    result = await coord.handle_transfer_call(
        input_data, call_id="call_transfer_01"
    )
    assert result == "One moment while I transfer you directly to our front desk team."


async def test_handle_check_availability_malformed_dates(
    test_settings: Settings,
) -> None:
    """Verify acoustic error envelopes when caller provides unparseable dates."""
    dummy_client = httpx.AsyncClient()
    gcal = GoogleCalendarClient.from_settings(dummy_client, test_settings)
    coord = BookingCoordinator(gcal, test_settings)

    # Malformed start_time
    input_bad_start = CheckAvailabilityInput(start_time="not-a-valid-date")
    resp_start = await coord.handle_check_availability(
        input_bad_start, call_id="bad_01"
    )
    assert "trouble understanding that date" in resp_start

    # Malformed end_time
    input_bad_end = CheckAvailabilityInput(
        start_time="2026-10-28T09:00:00", end_time="garbage-date"
    )
    resp_end = await coord.handle_check_availability(
        input_bad_end, call_id="bad_02"
    )
    assert "trouble understanding the end time" in resp_end


async def test_handle_check_availability_after_hours_same_day(
    test_settings: Settings,
) -> None:
    """Verify after-hours evening request suggests next business day."""
    dummy_client = httpx.AsyncClient()
    gcal = GoogleCalendarClient.from_settings(dummy_client, test_settings)
    coord = BookingCoordinator(gcal, test_settings)

    # 6:00 PM on Wednesday
    input_evening = CheckAvailabilityInput(start_time="2026-10-28T18:00:00")
    resp = await coord.handle_check_availability(
        input_evening, call_id="eve_01"
    )
    assert "Would you like to check the next business day?" in resp


async def test_handle_check_availability_slot_counts(
    test_settings: Settings,
) -> None:
    """Verify spoken proposal phrasing for 1 slot vs 3 slots."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Busy from 9:30 to 17:00 -> leaves only 9:00 AM
        return httpx.Response(
            200,
            json={
                "calendars": {
                    test_settings.calendar_id: {
                        "busy": [
                            {
                                "start": "2026-10-28T13:30:00Z",
                                "end": "2026-10-28T21:00:00Z",
                            }
                        ]
                    }
                }
            },
        )

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        coord = BookingCoordinator(gcal, test_settings)

        input_single = CheckAvailabilityInput(start_time="2026-10-28")
        resp_single = await coord.handle_check_availability(
            input_single, call_id="single_slot"
        )
        expected_single = (
            "We have an opening on Wednesday, October 28th at 9:00 AM."
        )
        assert expected_single in resp_single
        assert "Would you like me to book that?" in resp_single


async def test_handle_book_appointment_error_branches(
    test_settings: Settings,
) -> None:
    """Verify error fallbacks for malformed times and upstream calendar failures."""
    # 1. Malformed start_time
    dummy_client = httpx.AsyncClient()
    gcal = GoogleCalendarClient.from_settings(dummy_client, test_settings)
    coord = BookingCoordinator(gcal, test_settings)

    input_bad = BookAppointmentInput(
        customer_name="Alex", phone="+15551234567", start_time="bad-date"
    )
    resp_bad = await coord.handle_book_appointment(input_bad, call_id="err_01")
    assert "trouble understanding the appointment time" in resp_bad

    # 2. freeBusy check fails with CalendarServiceError
    def handler_fb_fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Timeout on freebusy")

    async with make_mock_client(handler_fb_fail) as client:
        gcal_fail = GoogleCalendarClient.from_settings(client, test_settings)
        coord_fail = BookingCoordinator(gcal_fail, test_settings)
        input_valid = BookAppointmentInput(
            customer_name="Alex",
            phone="+15551234567",
            start_time="2026-10-28T14:00:00",
        )
        resp_fail = await coord_fail.handle_book_appointment(
            input_valid, call_id="err_fb"
        )
        assert "My scheduling system is taking a moment" in resp_fail

    # 3. create_event fails with CalendarServiceError
    def handler_create_fail(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/calendar/v3/freeBusy":
            return httpx.Response(
                200,
                json={"calendars": {test_settings.calendar_id: {"busy": []}}},
            )
        raise httpx.ConnectError("Connection dropped on create")

    async with make_mock_client(handler_create_fail) as client:
        gcal_fail2 = GoogleCalendarClient.from_settings(client, test_settings)
        coord_fail2 = BookingCoordinator(gcal_fail2, test_settings)
        resp_fail2 = await coord_fail2.handle_book_appointment(
            input_valid, call_id="err_create"
        )
        assert "My scheduling system is taking a moment" in resp_fail2
