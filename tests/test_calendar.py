"""Deterministic unit tests for GoogleCalendarClient with wire transport mock."""

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from app.config import Settings
from app.errors import (
    CalendarServiceError,
    SSRFError,
    TimeRangeError,
)
from app.services.calendar import GoogleCalendarClient, _is_transient_error


def make_mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    """Create an async httpx client backed by a mock transport."""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


async def test_client_init_and_from_settings(test_settings: Settings) -> None:
    """Verify default initialization and from_settings factory method."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with make_mock_client(handler) as client:
        instance = GoogleCalendarClient.from_settings(client, test_settings)
        assert instance._calendar_id == test_settings.calendar_id
        assert instance._base_url == "https://www.googleapis.com/calendar/v3"
        assert instance._token.get_secret_value() == "test-google-token"

        # Verify fallback default token
        custom = GoogleCalendarClient(
            client=client,
            calendar_id="test_cal",
            base_url="https://www.googleapis.com/calendar/v3",
        )
        assert custom._token.get_secret_value() == "mock-token"


async def test_ssrf_rejection_metadata_ip() -> None:
    """Verify SSRF defense blocks cloud metadata IP and internal domains."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with make_mock_client(handler) as client:
        with pytest.raises(SSRFError, match="SSRF attempt blocked"):
            GoogleCalendarClient(
                client=client,
                calendar_id="test",
                base_url="http://169.254.169.254/calendar/v3",
            )

        with pytest.raises(SSRFError, match="SSRF attempt blocked"):
            GoogleCalendarClient(
                client=client,
                calendar_id="test",
                base_url="https://metadata.google.internal/calendar/v3",
            )


async def test_ssrf_rejection_invalid_scheme() -> None:
    """Verify SSRF defense rejects non-http(s) URI schemes."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with make_mock_client(handler) as client:
        with pytest.raises(SSRFError, match="Invalid calendar URL scheme"):
            GoogleCalendarClient(
                client=client,
                calendar_id="test",
                base_url="ftp://calendar.google.com/calendar/v3",
            )


async def test_get_free_busy_success(test_settings: Settings) -> None:
    """Verify free/busy query parsing with wire mock."""
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        assert request.url.path == "/calendar/v3/freeBusy"
        assert request.headers["Authorization"] == "Bearer test-google-token"
        assert request.headers["Content-Type"] == "application/json"

        body = json.loads(request.content.decode("utf-8"))
        assert body["timeZone"] == "America/New_York"
        assert body["items"] == [{"id": test_settings.calendar_id}]

        response_payload = {
            "kind": "calendar#freeBusy",
            "timeMin": "2026-10-28T09:00:00Z",
            "timeMax": "2026-10-28T17:00:00Z",
            "calendars": {
                test_settings.calendar_id: {
                    "busy": [
                        {
                            "start": "2026-10-28T14:00:00Z",
                            "end": "2026-10-28T15:00:00Z",
                        },
                        {
                            "start": "2026-10-28T10:00:00Z",
                            "end": "2026-10-28T11:00:00Z",
                        },
                    ]
                }
            },
        }
        return httpx.Response(200, json=response_payload)

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        time_min = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
        time_max = datetime(2026, 10, 28, 17, 0, tzinfo=UTC)

        busy_slots = await gcal.get_free_busy(
            time_min=time_min,
            time_max=time_max,
            timezone="America/New_York",
        )

        assert len(captured_requests) == 1
        assert len(busy_slots) == 2
        # Verify slots are sorted chronologically
        assert busy_slots[0][0] == datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
        assert busy_slots[0][1] == datetime(2026, 10, 28, 11, 0, tzinfo=UTC)
        assert busy_slots[1][0] == datetime(2026, 10, 28, 14, 0, tzinfo=UTC)
        assert busy_slots[1][1] == datetime(2026, 10, 28, 15, 0, tzinfo=UTC)


async def test_calendar_privacy_no_leak_of_titles_or_attendees(
    test_settings: Settings,
) -> None:
    """Verify calendar privacy guardrail.
    Ensures freeBusy only parses intervals, never titles/PII.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # Simulate response with extra attributes that must not be parsed or stored
        response_payload = {
            "calendars": {
                test_settings.calendar_id: {
                    "busy": [
                        {
                            "start": "2026-10-28T13:00:00Z",
                            "end": "2026-10-28T14:00:00Z",
                            "summary": "Confidential Surgery - Patient Alex",
                            "attendees": [{"email": "alex@example.com"}],
                        }
                    ]
                }
            }
        }
        return httpx.Response(200, json=response_payload)

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        time_min = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
        time_max = datetime(2026, 10, 28, 17, 0, tzinfo=UTC)

        busy_slots = await gcal.get_free_busy(
            time_min=time_min,
            time_max=time_max,
            timezone="America/New_York",
        )

        assert len(busy_slots) == 1
        slot = busy_slots[0]
        # Verify result is strictly a 2-tuple of datetimes
        assert isinstance(slot, tuple)
        assert len(slot) == 2
        assert isinstance(slot[0], datetime)
        assert isinstance(slot[1], datetime)
        # Ensure no summary or attendee metadata exists on return types
        assert not hasattr(slot, "summary")
        assert not hasattr(slot, "attendees")


async def test_get_free_busy_time_range_error(test_settings: Settings) -> None:
    """Verify TimeRangeError raised when time_max <= time_min."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        dt = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

        with pytest.raises(TimeRangeError, match="must be strictly after"):
            await gcal.get_free_busy(
                time_min=dt,
                time_max=dt,
                timezone="America/New_York",
            )


async def test_get_free_busy_skips_malformed_items(
    test_settings: Settings,
) -> None:
    """Verify items missing start or end timestamps are gracefully skipped."""

    def handler(request: httpx.Request) -> httpx.Response:
        response_payload = {
            "calendars": {
                test_settings.calendar_id: {
                    "busy": [
                        {"start": "2026-10-28T10:00:00Z"},  # missing end
                        {"end": "2026-10-28T11:00:00Z"},  # missing start
                        {
                            "start": "2026-10-28T14:00:00Z",
                            "end": "2026-10-28T15:00:00Z",
                        },
                    ]
                }
            }
        }
        return httpx.Response(200, json=response_payload)

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        busy_slots = await gcal.get_free_busy(
            time_min=datetime(2026, 10, 28, 9, 0, tzinfo=UTC),
            time_max=datetime(2026, 10, 28, 17, 0, tzinfo=UTC),
            timezone="America/New_York",
        )
        assert len(busy_slots) == 1
        assert busy_slots[0][0] == datetime(2026, 10, 28, 14, 0, tzinfo=UTC)


async def test_get_free_busy_calendar_errors(test_settings: Settings) -> None:
    """Verify CalendarServiceError raised when calendar response contains errors."""

    def handler(request: httpx.Request) -> httpx.Response:
        response_payload = {
            "calendars": {
                test_settings.calendar_id: {
                    "errors": [{"reason": "notFound", "domain": "global"}]
                }
            }
        }
        return httpx.Response(200, json=response_payload)

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        with pytest.raises(CalendarServiceError, match="returned calendar errors"):
            await gcal.get_free_busy(
                time_min=datetime(2026, 10, 28, 9, 0, tzinfo=UTC),
                time_max=datetime(2026, 10, 28, 17, 0, tzinfo=UTC),
                timezone="America/New_York",
            )


async def test_get_free_busy_timeout(test_settings: Settings) -> None:
    """Verify CalendarServiceError raised on request timeout."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Read operation timed out")

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        with pytest.raises(CalendarServiceError, match="timed out"):
            await gcal.get_free_busy(
                time_min=datetime(2026, 10, 28, 9, 0, tzinfo=UTC),
                time_max=datetime(2026, 10, 28, 17, 0, tzinfo=UTC),
                timezone="America/New_York",
            )


async def test_get_free_busy_network_error(test_settings: Settings) -> None:
    """Verify CalendarServiceError raised on transport network error."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        with pytest.raises(CalendarServiceError, match="network error"):
            await gcal.get_free_busy(
                time_min=datetime(2026, 10, 28, 9, 0, tzinfo=UTC),
                time_max=datetime(2026, 10, 28, 17, 0, tzinfo=UTC),
                timezone="America/New_York",
            )


async def test_get_free_busy_upstream_500_retry_and_fail(
    test_settings: Settings,
) -> None:
    """Verify tenacity retries 5xx errors up to 3 times before failing closed."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, json={"error": "Internal Server Error"})

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        with pytest.raises(CalendarServiceError, match="failed with status 500"):
            await gcal.get_free_busy(
                time_min=datetime(2026, 10, 28, 9, 0, tzinfo=UTC),
                time_max=datetime(2026, 10, 28, 17, 0, tzinfo=UTC),
                timezone="America/New_York",
            )
        assert attempts == 3


async def test_get_free_busy_upstream_401_no_retry(
    test_settings: Settings,
) -> None:
    """Verify client errors (401 Unauthorized) are not retried."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"error": "Unauthorized"})

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        with pytest.raises(CalendarServiceError, match="failed with status 401"):
            await gcal.get_free_busy(
                time_min=datetime(2026, 10, 28, 9, 0, tzinfo=UTC),
                time_max=datetime(2026, 10, 28, 17, 0, tzinfo=UTC),
                timezone="America/New_York",
            )
        assert attempts == 1


async def test_create_event_success(test_settings: Settings) -> None:
    """Verify appointment insertion returning created event ID."""
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        # Verify URL-encoded calendar_id path on the raw wire
        assert b"%40" in request.url.raw_path
        assert b"@" not in request.url.raw_path
        assert request.headers["Authorization"] == "Bearer test-google-token"

        body = json.loads(request.content.decode("utf-8"))
        assert body["summary"] == "Dental Cleaning - Alex Rivera"
        assert "Alex Rivera" in body["description"]
        assert "+15551234567" in body["description"]
        assert body["start"]["dateTime"] == "2026-10-28T14:00:00+00:00"
        assert body["end"]["dateTime"] == "2026-10-28T15:00:00+00:00"

        return httpx.Response(201, json={"id": "evt_conf_12345"})

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        event_id = await gcal.create_event(
            customer_name="Alex Rivera",
            phone="+15551234567",
            start_time=datetime(2026, 10, 28, 14, 0, tzinfo=UTC),
            end_time=datetime(2026, 10, 28, 15, 0, tzinfo=UTC),
            service_type="Dental Cleaning",
        )
        assert event_id == "evt_conf_12345"
        assert len(captured_requests) == 1


async def test_create_event_time_range_error(test_settings: Settings) -> None:
    """Verify TimeRangeError raised when end_time is before or equal to start_time."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "evt_test"})

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        dt = datetime(2026, 10, 28, 14, 0, tzinfo=UTC)
        with pytest.raises(TimeRangeError, match="must be strictly after"):
            await gcal.create_event(
                customer_name="Alex Rivera",
                phone="+15551234567",
                start_time=dt,
                end_time=dt,
                service_type="General Consultation",
            )


async def test_create_event_missing_id_in_response(
    test_settings: Settings,
) -> None:
    """Verify CalendarServiceError raised when response lacks event ID."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "confirmed"})

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        with pytest.raises(CalendarServiceError, match="missing event id"):
            await gcal.create_event(
                customer_name="Alex Rivera",
                phone="+15551234567",
                start_time=datetime(2026, 10, 28, 14, 0, tzinfo=UTC),
                end_time=datetime(2026, 10, 28, 15, 0, tzinfo=UTC),
                service_type="General Consultation",
            )


async def test_create_event_timeout(test_settings: Settings) -> None:
    """Verify CalendarServiceError raised on create_event timeout."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.WriteTimeout("Write timeout")

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        with pytest.raises(CalendarServiceError, match="timed out"):
            await gcal.create_event(
                customer_name="Alex Rivera",
                phone="+15551234567",
                start_time=datetime(2026, 10, 28, 14, 0, tzinfo=UTC),
                end_time=datetime(2026, 10, 28, 15, 0, tzinfo=UTC),
                service_type="General Consultation",
            )


async def test_create_event_http_error(test_settings: Settings) -> None:
    """Verify CalendarServiceError raised on create_event HTTP failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "Service Unavailable"})

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        with pytest.raises(CalendarServiceError, match="failed with status 503"):
            await gcal.create_event(
                customer_name="Alex Rivera",
                phone="+15551234567",
                start_time=datetime(2026, 10, 28, 14, 0, tzinfo=UTC),
                end_time=datetime(2026, 10, 28, 15, 0, tzinfo=UTC),
                service_type="General Consultation",
            )


async def test_create_event_network_error(test_settings: Settings) -> None:
    """Verify CalendarServiceError raised on create_event network drop."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection dropped")

    async with make_mock_client(handler) as client:
        gcal = GoogleCalendarClient.from_settings(client, test_settings)
        with pytest.raises(CalendarServiceError, match="network error"):
            await gcal.create_event(
                customer_name="Alex Rivera",
                phone="+15551234567",
                start_time=datetime(2026, 10, 28, 14, 0, tzinfo=UTC),
                end_time=datetime(2026, 10, 28, 15, 0, tzinfo=UTC),
                service_type="General Consultation",
            )


def test_is_transient_error_predicate() -> None:
    """Verify error classifier predicate for tenacity retry decisions."""
    # Transient network errors
    assert _is_transient_error(httpx.ReadTimeout("timeout")) is True
    assert _is_transient_error(httpx.ConnectError("refused")) is True

    # 429 Too Many Requests
    req = httpx.Request("POST", "https://example.com")
    res_429 = httpx.Response(429, request=req)
    err_429 = httpx.HTTPStatusError("429", request=req, response=res_429)
    assert _is_transient_error(err_429) is True

    # 5xx Server Errors
    res_500 = httpx.Response(500, request=req)
    err_500 = httpx.HTTPStatusError("500", request=req, response=res_500)
    assert _is_transient_error(err_500) is True

    # 4xx Client Errors (non-transient)
    res_400 = httpx.Response(400, request=req)
    err_400 = httpx.HTTPStatusError("400", request=req, response=res_400)
    assert _is_transient_error(err_400) is False

    # Standard exceptions (non-transient)
    assert _is_transient_error(ValueError("value error")) is False
