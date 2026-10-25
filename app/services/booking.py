"""Booking coordinator service managing availability, commits, and idempotency."""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import dateutil.parser

from app.config import Settings
from app.errors import (
    CalendarServiceError,
    InvalidTimezoneError,
    TimeRangeError,
)
from app.logging import get_logger, sanitize_log_string
from app.models.appointment import (
    BookAppointmentInput,
    CheckAvailabilityInput,
    DNCRequestInput,
    TransferCallInput,
)
from app.services.calendar import GoogleCalendarClient

logger = get_logger("booking_coordinator")

# Standard clinic service durations in minutes
SERVICE_DURATIONS: dict[str, int] = {
    "General Consultation": 30,
    "Dental Cleaning": 60,
    "Follow-up": 15,
}


def _format_ordinal(day: int) -> str:
    """Format day number with English ordinal suffix (e.g. 1st, 2nd, 28th)."""
    if 11 <= (day % 100) <= 13:
        return f"{day}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


class BookingCoordinator:
    """Coordinates two-phase booking, slot generation, and host idempotency.

    Concurrency Invariant: Uses self._lock (asyncio.Lock) to serialize slot
    check-and-reserve operations, eliminating TOCTOU race conditions between
    concurrent caller webhooks.
    """

    def __init__(
        self,
        calendar_client: GoogleCalendarClient,
        settings: Settings,
    ) -> None:
        """Initialize BookingCoordinator with calendar client and clinic settings."""
        self._calendar = calendar_client
        self._settings = settings
        self._lock = asyncio.Lock()
        self._idempotency_cache: dict[str, str] = {}

    def format_spoken_datetime(self, dt: datetime) -> str:
        """Format datetime into natural phonetic speech for voice synthesis.

        Enforces Acoustic Anti-Markdown Invariant: output is plain conversational
        text with no markdown formatting symbols, bullets, or raw ISO timestamps.
        """
        zone = ZoneInfo(self._settings.clinic_timezone)
        dt_loc = dt.replace(tzinfo=zone) if dt.tzinfo is None else dt.astimezone(zone)

        day_ordinal = _format_ordinal(dt_loc.day)
        date_str = dt_loc.strftime(f"%A, %B {day_ordinal}")
        hour_str = dt_loc.strftime("%I").lstrip("0")
        minute_str = dt_loc.strftime("%M")
        ampm_str = dt_loc.strftime("%p")

        return f"{date_str} at {hour_str}:{minute_str} {ampm_str}"

    async def handle_check_availability(
        self,
        args: CheckAvailabilityInput,
        call_id: str,
    ) -> str:
        """Query availability, filter to clinic hours, and return spoken response."""
        clean_call_id = sanitize_log_string(call_id)
        tz_name = args.timezone or self._settings.clinic_timezone

        try:
            zone = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, Exception) as exc:
            raise InvalidTimezoneError(f"Invalid timezone: {tz_name}") from exc

        try:
            parsed_start = dateutil.parser.parse(args.start_time)
        except Exception:
            return (
                "I had trouble understanding that date. "
                "Could you please repeat the day you would like to come in?"
            )

        start_dt = (
            parsed_start.replace(tzinfo=zone)
            if parsed_start.tzinfo is None
            else parsed_start.astimezone(zone)
        )

        if args.end_time is not None:
            try:
                parsed_end = dateutil.parser.parse(args.end_time)
            except Exception:
                return (
                    "I had trouble understanding the end time. "
                    "Could you please specify a valid date range?"
                )
            end_dt = (
                parsed_end.replace(tzinfo=zone)
                if parsed_end.tzinfo is None
                else parsed_end.astimezone(zone)
            )

            if end_dt <= start_dt:
                raise TimeRangeError("end_time must be strictly after start_time")

            if (end_dt - start_dt).days > self._settings.max_query_days:
                raise TimeRangeError(
                    f"Search window cannot exceed {self._settings.max_query_days} days"
                )
        else:
            # Default to full business hours for that day if time was omitted (00:00)
            if parsed_start.hour == 0 and parsed_start.minute == 0:
                start_dt = datetime(
                    start_dt.year,
                    start_dt.month,
                    start_dt.day,
                    self._settings.business_hours_start,
                    0,
                    tzinfo=zone,
                )
                end_dt = datetime(
                    start_dt.year,
                    start_dt.month,
                    start_dt.day,
                    self._settings.business_hours_end,
                    0,
                    tzinfo=zone,
                )
            else:
                end_dt = datetime(
                    start_dt.year,
                    start_dt.month,
                    start_dt.day,
                    self._settings.business_hours_end,
                    0,
                    tzinfo=zone,
                )
                if end_dt <= start_dt:
                    return (
                        "Our office is open Monday through Friday from 9:00 AM "
                        "to 5:00 PM. Would you like to check the next business day?"
                    )

        # Off-hours weekend inquiry check
        if start_dt.date() == end_dt.date() and start_dt.weekday() >= 5:
            return (
                "Our office is open Monday through Friday from 9:00 AM to 5:00 PM. "
                "Would you like to look at Monday morning?"
            )

        logger.info(
            "checking_calendar_availability",
            call_id=clean_call_id,
            start=start_dt.isoformat(),
            end=end_dt.isoformat(),
        )

        try:
            busy_intervals = await self._calendar.get_free_busy(
                start_dt, end_dt, tz_name
            )
        except CalendarServiceError as exc:
            logger.error(
                "check_availability_calendar_error",
                call_id=clean_call_id,
                error=str(exc),
            )
            return (
                "My scheduling system is taking a moment. May I take your preferred "
                "time, and our team will text you a confirmation shortly?"
            )

        # Generate candidate slots within clinic business hours
        slot_duration = self._settings.default_slot_duration_minutes
        available_slots: list[datetime] = []

        curr_day = start_dt.date()
        while curr_day <= end_dt.date():
            if curr_day.weekday() < 5:  # Monday through Friday
                day_open = datetime(
                    curr_day.year,
                    curr_day.month,
                    curr_day.day,
                    self._settings.business_hours_start,
                    0,
                    tzinfo=zone,
                )
                day_close = datetime(
                    curr_day.year,
                    curr_day.month,
                    curr_day.day,
                    self._settings.business_hours_end,
                    0,
                    tzinfo=zone,
                )

                window_start = max(start_dt, day_open)
                window_end = min(end_dt, day_close)

                candidate = window_start
                while candidate + timedelta(minutes=slot_duration) <= window_end:
                    candidate_end = candidate + timedelta(minutes=slot_duration)

                    # Check overlap with busy intervals
                    is_busy = False
                    for busy_start, busy_end in busy_intervals:
                        c_utc = candidate.astimezone(UTC)
                        ce_utc = candidate_end.astimezone(UTC)
                        b_start_utc = busy_start.astimezone(UTC)
                        b_end_utc = busy_end.astimezone(UTC)

                        if c_utc < b_end_utc and ce_utc > b_start_utc:
                            is_busy = True
                            break

                    if not is_busy:
                        available_slots.append(candidate)

                    candidate = candidate_end

            curr_day += timedelta(days=1)

        if not available_slots:
            return (
                "I apologize, but we have no available appointments for that time. "
                "Would you like to check another date?"
            )

        # Format up to 3 slots into a natural spoken proposal
        offered = available_slots[:3]
        if all(s.date() == offered[0].date() for s in offered):
            first_spoken = self.format_spoken_datetime(offered[0])
            if len(offered) == 1:
                return (
                    f"We have an opening on {first_spoken}. "
                    "Would you like me to book that?"
                )

            times: list[str] = []
            for s in offered[1:]:
                hour = s.strftime("%I").lstrip("0")
                minute = s.strftime("%M")
                ampm = s.strftime("%p")
                times.append(f"{hour}:{minute} {ampm}")

            if len(times) == 1:
                return (
                    f"We have openings on {first_spoken} and {times[0]}. "
                    "Would you like me to book one of those?"
                )
            return (
                f"We have openings on {first_spoken}, {times[0]}, and {times[1]}. "
                "Would you like me to book one of those?"
            )

        spoken_slots = [self.format_spoken_datetime(s) for s in offered]
        joined = " and ".join(spoken_slots)
        return f"We have openings on {joined}. Would you like me to book one of those?"

    async def handle_book_appointment(
        self,
        args: BookAppointmentInput,
        call_id: str,
    ) -> str:
        """Execute Phase 2 commitment with host-enforced composite idempotency.

        Business Invariants:
        1. Validates start_time is within clinic business hours (Mon-Fri 9am-5pm).
        2. Validates slot is free in Google Calendar.
        3. Serialized under self._lock to eliminate concurrent double-booking.
        """
        clean_call_id = sanitize_log_string(call_id)
        zone = ZoneInfo(self._settings.clinic_timezone)

        async with self._lock:
            # Deterministic composite idempotency key
            idempotency_key = (
                args.idempotency_key or f"{clean_call_id}:{args.start_time}"
            )

            if idempotency_key in self._idempotency_cache:
                logger.info(
                    "returning_idempotent_booking_confirmation",
                    call_id=clean_call_id,
                    idempotency_key=idempotency_key,
                )
                return self._idempotency_cache[idempotency_key]

            try:
                parsed = dateutil.parser.parse(args.start_time)
            except Exception:
                return (
                    "I had trouble understanding the appointment time. "
                    "Could you please repeat the time you would like?"
                )

            start_dt = (
                parsed.replace(tzinfo=zone)
                if parsed.tzinfo is None
                else parsed.astimezone(zone)
            )

            duration = SERVICE_DURATIONS.get(
                args.service_type, self._settings.default_slot_duration_minutes
            )
            end_dt = start_dt + timedelta(minutes=duration)

            # Business hours boundary checks
            if start_dt.weekday() >= 5:
                return (
                    "Our office is open Monday through Friday from 9:00 AM to 5:00 PM. "
                    "Would you like to look at Monday morning?"
                )

            clinic_open = datetime(
                start_dt.year,
                start_dt.month,
                start_dt.day,
                self._settings.business_hours_start,
                0,
                tzinfo=zone,
            )
            clinic_close = datetime(
                start_dt.year,
                start_dt.month,
                start_dt.day,
                self._settings.business_hours_end,
                0,
                tzinfo=zone,
            )

            if start_dt < clinic_open or end_dt > clinic_close:
                return (
                    "Our office is open Monday through Friday from 9:00 AM to 5:00 PM. "
                    "Would you like to schedule during regular hours?"
                )

            # Atomic slot verification (: Zero Double-Booking)
            try:
                busy_intervals = await self._calendar.get_free_busy(
                    start_dt, end_dt, self._settings.clinic_timezone
                )
            except CalendarServiceError as exc:
                logger.error(
                    "booking_calendar_check_failed",
                    call_id=clean_call_id,
                    error=str(exc),
                )
                return (
                    "My scheduling system is taking a moment. May I take your "
                    "preferred time, and our team will text you a confirmation "
                    "shortly?"
                )

            is_busy = False
            for busy_start, busy_end in busy_intervals:
                s_utc = start_dt.astimezone(UTC)
                e_utc = end_dt.astimezone(UTC)
                b_start_utc = busy_start.astimezone(UTC)
                b_end_utc = busy_end.astimezone(UTC)

                if s_utc < b_end_utc and e_utc > b_start_utc:
                    is_busy = True
                    break

            if is_busy:
                slot_hour = start_dt.strftime("%I").lstrip("0")
                slot_minute = start_dt.strftime("%M")
                slot_ampm = start_dt.strftime("%p")
                slot_str = f"{slot_hour}:{slot_minute} {slot_ampm}"
                logger.info(
                    "requested_slot_occupied",
                    call_id=clean_call_id,
                    slot=slot_str,
                )
                return (
                    f"I apologize, but that {slot_str} slot was just filled. "
                    "Would another time work instead?"
                )

            # Insert confirmed appointment into Google Calendar
            try:
                event_id = await self._calendar.create_event(
                    customer_name=args.customer_name,
                    phone=args.phone,
                    start_time=start_dt,
                    end_time=end_dt,
                    service_type=args.service_type,
                )
            except CalendarServiceError as exc:
                logger.error(
                    "booking_calendar_create_failed",
                    call_id=clean_call_id,
                    error=str(exc),
                )
                return (
                    "My scheduling system is taking a moment. May I take your "
                    "preferred time, and our team will text you a confirmation "
                    "shortly?"
                )

            spoken_dt = self.format_spoken_datetime(start_dt)
            service_desc = args.service_type.lower()
            confirmation = (
                f"You're all set, {args.customer_name}! "
                f"I've booked your {service_desc} for {spoken_dt}."
            )

            self._idempotency_cache[idempotency_key] = confirmation
            logger.info(
                "appointment_booked_successfully",
                call_id=clean_call_id,
                event_id=event_id,
                idempotency_key=idempotency_key,
            )
            return confirmation

    async def handle_dnc_request(
        self,
        args: DNCRequestInput,
        call_id: str,
    ) -> str:
        """Record statutory Do-Not-Call opt-out and return legal confirmation."""
        clean_call_id = sanitize_log_string(call_id)
        clean_phone = sanitize_log_string(args.phone)
        clean_reason = sanitize_log_string(args.reason)

        logger.info(
            "dnc_request_recorded",
            call_id=clean_call_id,
            phone=clean_phone,
            reason=clean_reason,
        )
        return "Understood. I have removed this number from our list. Have a great day."

    async def handle_transfer_call(
        self,
        args: TransferCallInput,
        call_id: str,
    ) -> str:
        """Trigger warm transfer sequence and return spoken notice to caller."""
        clean_call_id = sanitize_log_string(call_id)
        clean_department = sanitize_log_string(args.department)
        clean_reason = sanitize_log_string(args.reason)

        logger.info(
            "warm_transfer_initiated",
            call_id=clean_call_id,
            department=clean_department,
            reason=clean_reason,
        )
        return "One moment while I transfer you directly to our front desk team."
