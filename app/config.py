"""Application settings and environment configuration."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import SSRFError

BLOCKED_HOSTS: frozenset[str] = frozenset(
    {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata.internal",
        "instance-data",
    }
)


class Settings(BaseSettings):
    """Centralized application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Core Environment & Paths
    environment: str = Field(
        default="development",
        validation_alias=AliasChoices("ENVIRONMENT", "environment"),
    )
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("LOG_LEVEL", "log_level"),
    )
    project_root: Path = Field(default_factory=lambda: Path.cwd().resolve())

    # Telephony & Webhook Security
    vapi_secret_token: SecretStr = Field(
        default=SecretStr("dev-vapi-secret"),
        validation_alias=AliasChoices("VAPI_SECRET_TOKEN", "vapi_secret_token"),
    )
    vapi_api_key: SecretStr = Field(
        default=SecretStr("mock-vapi-key"),
        validation_alias=AliasChoices("VAPI_API_KEY", "vapi_api_key"),
    )
    vapi_assistant_id: str = Field(
        default="asst_dev123",
        validation_alias=AliasChoices("VAPI_ASSISTANT_ID", "vapi_assistant_id"),
    )

    # Google Calendar Integration (Least privilege service account / OAuth)
    calendar_id: str = Field(
        default="primary",
        validation_alias=AliasChoices("CALENDAR_ID", "calendar_id"),
    )
    google_calendar_token: SecretStr = Field(
        default=SecretStr("mock-token"),
        validation_alias=AliasChoices("GOOGLE_CALENDAR_TOKEN", "google_calendar_token"),
    )
    google_calendar_base_url: str = Field(
        default="https://www.googleapis.com/calendar/v3",
        validation_alias=AliasChoices(
            "GOOGLE_CALENDAR_BASE_URL", "google_calendar_base_url"
        ),
    )

    # Scheduling Business Constraints
    clinic_name: str = Field(
        default="Acme Clinic",
        validation_alias=AliasChoices("CLINIC_NAME", "clinic_name"),
    )
    clinic_timezone: str = Field(
        default="America/New_York",
        validation_alias=AliasChoices("CLINIC_TIMEZONE", "clinic_timezone"),
    )
    business_hours_start: int = Field(
        default=9,
        ge=0,
        le=23,
        validation_alias=AliasChoices("BUSINESS_HOURS_START", "business_hours_start"),
    )
    business_hours_end: int = Field(
        default=17,
        ge=0,
        le=23,
        validation_alias=AliasChoices("BUSINESS_HOURS_END", "business_hours_end"),
    )
    default_slot_duration_minutes: int = Field(
        default=30,
        ge=15,
        le=120,
        validation_alias=AliasChoices(
            "DEFAULT_SLOT_DURATION_MINUTES", "default_slot_duration_minutes"
        ),
    )
    max_query_days: int = Field(
        default=14,
        ge=1,
        le=30,
        validation_alias=AliasChoices("MAX_QUERY_DAYS", "max_query_days"),
    )

    # Latency & Telephony Dead Air Budgets
    webhook_timeout_seconds: float = Field(
        default=2.5,
        gt=0.5,
        le=5.0,
        validation_alias=AliasChoices(
            "WEBHOOK_TIMEOUT_SECONDS", "webhook_timeout_seconds"
        ),
    )

    @field_validator("google_calendar_base_url")
    @classmethod
    def validate_calendar_url(cls, v: str) -> str:
        """Validate calendar URL scheme and prevent SSRF to metadata endpoints."""
        parsed = urlparse(v)
        if parsed.scheme not in ("https", "http"):
            raise SSRFError(f"Invalid calendar URL scheme: {parsed.scheme}")
        hostname = (parsed.hostname or "").lower()
        if hostname in BLOCKED_HOSTS:
            raise SSRFError(f"SSRF attempt blocked: access to {hostname} is forbidden")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Lazy singleton accessor for application settings."""
    return Settings()
