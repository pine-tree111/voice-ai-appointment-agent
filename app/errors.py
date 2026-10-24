"""Domain exception hierarchy for the voice-ai-appointment-agent service."""


class VoiceAgentError(Exception):
    """Base exception for all voice-ai-appointment-agent domain errors."""


class SecurityViolationError(VoiceAgentError):
    """Base exception for authentication failures, injection attacks, or SSRF."""


class WebhookAuthError(SecurityViolationError):
    """Raised when the inbound X-Vapi-Secret header is missing or invalid."""


class SSRFError(SecurityViolationError):
    """Raised when a calendar endpoint targets private, loopback, or metadata IPs."""


class SlotUnavailableError(VoiceAgentError):
    """Raised when a requested appointment slot is already occupied or unavailable."""


class DoubleBookingError(VoiceAgentError):
    """Raised when an identical booking idempotency key is submitted."""


class CalendarServiceError(VoiceAgentError):
    """Raised when upstream Google Calendar API returns an error or times out."""


class DNCRequestedError(VoiceAgentError):
    """Raised when a caller explicitly requests statutory Do-Not-Call removal."""


class EscalationRequiredError(VoiceAgentError):
    """Raised when a caller requests human transfer or escalation thresholds are met."""


class InvalidTimezoneError(VoiceAgentError):
    """Raised when an unparseable or unrecognized timezone string is specified."""


class TimeRangeError(VoiceAgentError):
    """Raised when an availability query range violates business boundary limits."""
