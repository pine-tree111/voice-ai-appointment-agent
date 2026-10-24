"""Pytest fixtures for voice-ai-appointment-agent test suites."""

from collections.abc import Generator

import pytest
from pydantic import SecretStr

from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def reset_settings_cache() -> Generator[None, None, None]:
    """Clear LRU cache on get_settings before and after each test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def test_settings() -> Settings:
    """Fixture providing isolated test settings."""
    return Settings(
        environment="test",
        log_level="DEBUG",
        vapi_secret_token=SecretStr("test-vapi-secret"),
        vapi_api_key=SecretStr("test-vapi-key"),
        vapi_assistant_id="asst_test_123",
        calendar_id="test_calendar@group.calendar.google.com",
        google_calendar_token=SecretStr("test-google-token"),
        google_calendar_base_url="https://www.googleapis.com/calendar/v3",
        clinic_name="Test Clinic",
        clinic_timezone="America/New_York",
        business_hours_start=9,
        business_hours_end=17,
        default_slot_duration_minutes=30,
        max_query_days=14,
        webhook_timeout_seconds=2.5,
    )
