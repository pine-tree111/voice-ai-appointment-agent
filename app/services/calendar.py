"""Google Calendar API client for free/busy scheduling and appointment creation."""

import ipaddress
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from pydantic import SecretStr
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import BLOCKED_HOSTS, Settings
from app.errors import (
    CalendarServiceError,
    SSRFError,
    TimeRangeError,
)
from app.logging import get_logger, sanitize_log_string

logger = get_logger("calendar_client")


def _is_transient_error(exc: BaseException) -> bool:
    """Predicate determining if an exception represents a retryable transient failure.

    Retries solely network timeouts, connection drops, and upstream 429/5xx errors.
    Explicitly avoids retrying client errors (400, 401, 403, 404).
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class GoogleCalendarClient:
    """Async Google Calendar REST API client operating over httpx.AsyncClient.

    Enforces the Calendar Privacy Guardrail: queries strictly freeBusy intervals, never
    reading, storing, or exposing existing event titles, descriptions, or attendee PII.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        calendar_id: str,
        base_url: str,
        token: SecretStr | None = None,
        timeout_seconds: float = 2.5,
    ) -> None:
        """Initialize GoogleCalendarClient with security validation and credentials.

        Args:
            client: Shared async HTTP client for non-blocking I/O.
            calendar_id: Primary calendar ID or service account delegate calendar email.
            base_url: Base URL for Google Calendar v3 API.
            token: Secret OAuth / service account bearer token.
                Defaults to "mock-token".
            timeout_seconds: Hard timeout per request, default 2.5s.

        Raises:
            SSRFError: If base_url targets loopback, cloud metadata, or invalid schemes.
        """
        self._client = client
        self._calendar_id = calendar_id
        self._base_url = self._validate_base_url(base_url)
        self._token = token if token is not None else SecretStr("mock-token")
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_settings(
        cls,
        client: httpx.AsyncClient,
        settings: Settings,
    ) -> "GoogleCalendarClient":
        """Factory creating a GoogleCalendarClient from application settings."""
        return cls(
            client=client,
            calendar_id=settings.calendar_id,
            base_url=settings.google_calendar_base_url,
            token=settings.google_calendar_token,
            timeout_seconds=settings.webhook_timeout_seconds,
        )

    def _validate_base_url(self, url: str) -> str:
        """Validate URL scheme and reject metadata and private/reserved IP endpoints."""
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http"):
            raise SSRFError(f"Invalid calendar URL scheme: {parsed.scheme}")
        hostname = (parsed.hostname or "").lower()
        if hostname in BLOCKED_HOSTS:
            raise SSRFError(f"SSRF attempt blocked: access to {hostname} is forbidden")
        try:
            addr = ipaddress.ip_address(hostname)
            if (
                addr.is_private
                or addr.is_loopback
                or addr.is_link_local
                or addr.is_reserved
            ):
                raise SSRFError(
                    f"Blocked: {hostname} is a private/reserved address"
                )
        except ValueError:
            pass  # Not an IP literal; hostname already checked against BLOCKED_HOSTS
        return url.rstrip("/")

    @retry(
        retry=retry_if_exception(_is_transient_error),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.1, min=0.1, max=0.5),
        reraise=True,
    )
    async def _post_with_retry(
        self,
        url: str,
        json_body: dict[str, Any],
    ) -> httpx.Response:
        """Execute POST request with capped exponential backoff on transient errors."""
        headers = {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "Content-Type": "application/json",
        }
        response = await self._client.post(
            url,
            json=json_body,
            headers=headers,
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        return response

    async def get_free_busy(
        self,
        time_min: datetime,
        time_max: datetime,
        timezone: str,
    ) -> list[tuple[datetime, datetime]]:
        """Query free/busy intervals from Google Calendar.

        Calendar Privacy Guardrail: Strictly queries freeBusy.query and
        returns busy intervals. Never retrieves, parses, or exposes existing
        event titles, descriptions, or PII.

        Args:
            time_min: Start boundary of the availability search window.
            time_max: End boundary of the availability search window.
            timezone: IANA timezone identifier for the query.

        Returns:
            List of (start, end) datetime tuples representing busy blocks.

        Raises:
            TimeRangeError: If time_max is not strictly greater than time_min.
            CalendarServiceError: If the Google Calendar API returns an error
                or times out.
        """
        if time_max <= time_min:
            raise TimeRangeError("time_max must be strictly after time_min")

        url = f"{self._base_url}/freeBusy"
        payload = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "timeZone": timezone,
            "items": [{"id": self._calendar_id}],
        }

        sanitized_tz = sanitize_log_string(timezone)
        logger.info(
            "querying_calendar_free_busy",
            time_min=time_min.isoformat(),
            time_max=time_max.isoformat(),
            timezone=sanitized_tz,
        )

        try:
            response = await self._post_with_retry(url, payload)
        except httpx.TimeoutException as exc:
            logger.error("calendar_free_busy_timeout", error=str(exc))
            raise CalendarServiceError(
                "Google Calendar availability query timed out"
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.error("calendar_free_busy_http_error", status_code=status)
            raise CalendarServiceError(
                f"Google Calendar availability query failed with status {status}"
            ) from exc
        except httpx.RequestError as exc:
            logger.error("calendar_free_busy_network_error", error=str(exc))
            raise CalendarServiceError(
                f"Google Calendar availability query network error: {exc}"
            ) from exc

        data = response.json()
        calendars = data.get("calendars", {})
        calendar_data = calendars.get(self._calendar_id, {})

        errors: Sequence[dict[str, Any]] | None = calendar_data.get("errors")
        if errors:
            logger.error("calendar_returned_errors", errors=errors)
            raise CalendarServiceError(
                f"Google Calendar returned calendar errors: {errors}"
            )

        busy_list = calendar_data.get("busy", [])
        busy_intervals: list[tuple[datetime, datetime]] = []

        for item in busy_list:
            start_raw = item.get("start")
            end_raw = item.get("end")
            if not start_raw or not end_raw:
                continue
            start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            busy_intervals.append((start_dt, end_dt))

        busy_intervals.sort(key=lambda slot: slot[0])
        logger.info(
            "calendar_free_busy_success",
            busy_count=len(busy_intervals),
        )
        return busy_intervals

    async def create_event(
        self,
        customer_name: str,
        phone: str,
        start_time: datetime,
        end_time: datetime,
        service_type: str,
    ) -> str:
        """Insert a confirmed appointment into Google Calendar.

        Args:
            customer_name: Spoken caller name.
            phone: Caller phone number.
            start_time: Start datetime of appointment.
            end_time: End datetime of appointment.
            service_type: Clinic service requested.

        Returns:
            Created event ID string.

        Raises:
            TimeRangeError: If end_time is not strictly greater than start_time.
            CalendarServiceError: If the event creation fails or returns no ID.
        """
        if end_time <= start_time:
            raise TimeRangeError("end_time must be strictly after start_time")

        clean_name = sanitize_log_string(customer_name)
        clean_phone = sanitize_log_string(phone)
        clean_service = sanitize_log_string(service_type)

        encoded_calendar_id = quote(self._calendar_id, safe="")
        url = f"{self._base_url}/calendars/{encoded_calendar_id}/events"

        payload = {
            "summary": f"{clean_service} - {clean_name}",
            "description": (
                f"Appointment for {clean_name}\n"
                f"Phone: {clean_phone}\n"
                f"Service: {clean_service}\n"
                f"Booked via Voice AI Receptionist"
            ),
            "start": {
                "dateTime": start_time.isoformat(),
            },
            "end": {
                "dateTime": end_time.isoformat(),
            },
        }

        logger.info(
            "creating_calendar_appointment",
            service_type=clean_service,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
        )

        try:
            response = await self._post_with_retry(url, payload)
        except httpx.TimeoutException as exc:
            logger.error("calendar_create_event_timeout", error=str(exc))
            raise CalendarServiceError(
                "Google Calendar appointment creation timed out"
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.error("calendar_create_event_http_error", status_code=status)
            raise CalendarServiceError(
                f"Google Calendar appointment creation failed with status {status}"
            ) from exc
        except httpx.RequestError as exc:
            logger.error("calendar_create_event_network_error", error=str(exc))
            raise CalendarServiceError(
                f"Google Calendar appointment creation network error: {exc}"
            ) from exc

        event_data = response.json()
        event_id = event_data.get("id")
        if not event_id:
            logger.error("calendar_missing_event_id_in_response", data=event_data)
            raise CalendarServiceError("Google Calendar response missing event id")

        logger.info(
            "calendar_appointment_created_successfully",
            event_id=event_id,
        )
        return str(event_id)
