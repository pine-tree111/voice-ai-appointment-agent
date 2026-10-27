"""Full-wiring integration tests for /webhook/vapi and /health endpoints."""

from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings, get_settings
from app.main import app
from app.services.booking import BookingCoordinator
from app.services.calendar import GoogleCalendarClient


def make_gcal_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    """Create a client with MockTransport for wire-level Google Calendar simulation."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
async def integration_app(
    test_settings: Settings,
) -> AsyncIterator[FastAPI]:
    """Provide real FastAPI app with test settings and clean state per test."""
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.state.booking_coordinator = None

    yield app

    app.dependency_overrides.clear()
    app.state.booking_coordinator = None
    if hasattr(app.state, "http_client"):
        delattr(app.state, "http_client")


async def test_health_check_endpoint(integration_app: FastAPI) -> None:
    """Verify /health returns HTTP 200 and operational service status."""
    transport = httpx.ASGITransport(app=integration_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "service": "voice-ai-appointment-agent",
        }


async def test_webhook_unauthorized_missing_secret(
    integration_app: FastAPI,
) -> None:
    """Verify missing X-Vapi-Secret returns HTTP 401 Unauthorized"""
    transport = httpx.ASGITransport(app=integration_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {"message": {"type": "tool-calls", "toolCalls": []}}
        response = await client.post("/webhook/vapi", json=payload)
        assert response.status_code == 401
        assert "Unauthorized" in response.json()["detail"]


async def test_webhook_unauthorized_invalid_secret(
    integration_app: FastAPI,
) -> None:
    """Verify invalid X-Vapi-Secret returns HTTP 401 Unauthorized."""
    transport = httpx.ASGITransport(app=integration_app)
    headers = {"X-Vapi-Secret": "fraudulent-unauthorized-secret"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {"message": {"type": "tool-calls", "toolCalls": []}}
        response = await client.post("/webhook/vapi", json=payload, headers=headers)
        assert response.status_code == 401
        assert "Unauthorized" in response.json()["detail"]


async def test_webhook_lifecycle_event_ignored(
    integration_app: FastAPI,
) -> None:
    """Verify non-tool lifecycle events return HTTP 200 with empty results."""
    transport = httpx.ASGITransport(app=integration_app)
    headers = {"X-Vapi-Secret": "test-vapi-secret"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "message": {
                "type": "status-update",
                "call": {"id": "call_lifecycle_99"},
            }
        }
        response = await client.post("/webhook/vapi", json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"results": []}


async def test_webhook_unmapped_tool_fails_closed(
    integration_app: FastAPI,
) -> None:
    """Verify unmapped tool names fail closed to friendly voice error."""
    transport = httpx.ASGITransport(app=integration_app)
    headers = {"X-Vapi-Secret": "test-vapi-secret"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "message": {
                "type": "tool-calls",
                "toolCalls": [
                    {
                        "id": "unmapped_tool_01",
                        "type": "function",
                        "function": {
                            "name": "delete_all_calendar_entries",
                            "arguments": {},
                        },
                    }
                ],
                "call": {"id": "call_adversarial_01"},
            }
        }
        response = await client.post("/webhook/vapi", json=payload, headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["toolCallId"] == "unmapped_tool_01"
        assert (
            "only able to help with checking availability"
            in data["results"][0]["result"]
        )


async def test_webhook_check_availability_success(
    integration_app: FastAPI,
    test_settings: Settings,
) -> None:
    """Verify check_availability dispatch returning natural spoken proposal."""

    def gcal_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"calendars": {test_settings.calendar_id: {"busy": []}}},
        )

    gcal_client = make_gcal_transport(gcal_handler)
    integration_app.state.http_client = gcal_client
    calendar_client = GoogleCalendarClient.from_settings(gcal_client, test_settings)
    integration_app.state.booking_coordinator = BookingCoordinator(
        calendar_client, test_settings
    )

    transport = httpx.ASGITransport(app=integration_app)
    headers = {"X-Vapi-Secret": "test-vapi-secret"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "message": {
                "type": "tool-calls",
                "toolCalls": [
                    {
                        "id": "avail_tool_01",
                        "type": "function",
                        "function": {
                            "name": "check_availability",
                            "arguments": {"start_time": "2026-10-28"},
                        },
                    }
                ],
                "call": {"id": "call_avail_01"},
            }
        }
        response = await client.post("/webhook/vapi", json=payload, headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["toolCallId"] == "avail_tool_01"
        result_str = data["results"][0]["result"]
        assert "Wednesday, October 28th" in result_str
        assert "**" not in result_str


async def test_webhook_book_appointment_success(
    integration_app: FastAPI,
    test_settings: Settings,
) -> None:
    """Verify book_appointment dispatch creats event and returns spoken confirmation."""

    def gcal_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/calendar/v3/freeBusy":
            return httpx.Response(
                200,
                json={"calendars": {test_settings.calendar_id: {"busy": []}}},
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(201, json={"id": "evt_hook_123"})
        return httpx.Response(404)

    gcal_client = make_gcal_transport(gcal_handler)
    integration_app.state.http_client = gcal_client
    calendar_client = GoogleCalendarClient.from_settings(gcal_client, test_settings)
    integration_app.state.booking_coordinator = BookingCoordinator(
        calendar_client, test_settings
    )

    transport = httpx.ASGITransport(app=integration_app)
    headers = {"X-Vapi-Secret": "test-vapi-secret"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "message": {
                "type": "tool-calls",
                "toolCalls": [
                    {
                        "id": "book_tool_01",
                        "type": "function",
                        "function": {
                            "name": "book_appointment",
                            "arguments": {
                                "customer_name": "Alex Rivera",
                                "phone": "+15551234567",
                                "start_time": "2026-10-28T14:00:00",
                                "service_type": "Dental Cleaning",
                            },
                        },
                    }
                ],
                "call": {"id": "call_book_01"},
            }
        }
        response = await client.post("/webhook/vapi", json=payload, headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["toolCallId"] == "book_tool_01"
        assert "You're all set, Alex Rivera!" in data["results"][0]["result"]
        assert "Wednesday, October 28th at 2:00 PM" in data["results"][0]["result"]


async def test_webhook_book_appointment_idempotent_replay(
    integration_app: FastAPI,
    test_settings: Settings,
) -> None:
    """Verify duplicate booking dispatch returns cached response from coordinator."""
    events_created = 0

    def gcal_handler(request: httpx.Request) -> httpx.Response:
        nonlocal events_created
        if request.url.path == "/calendar/v3/freeBusy":
            return httpx.Response(
                200,
                json={"calendars": {test_settings.calendar_id: {"busy": []}}},
            )
        if request.url.path.endswith("/events"):
            events_created += 1
            return httpx.Response(201, json={"id": "evt_idemp_123"})
        return httpx.Response(404)

    gcal_client = make_gcal_transport(gcal_handler)
    integration_app.state.http_client = gcal_client
    calendar_client = GoogleCalendarClient.from_settings(gcal_client, test_settings)
    integration_app.state.booking_coordinator = BookingCoordinator(
        calendar_client, test_settings
    )

    transport = httpx.ASGITransport(app=integration_app)
    headers = {"X-Vapi-Secret": "test-vapi-secret"}
    payload = {
        "message": {
            "type": "tool-calls",
            "toolCalls": [
                {
                    "id": "book_tool_idemp",
                    "type": "function",
                    "function": {
                        "name": "book_appointment",
                        "arguments": {
                            "customer_name": "Alex Rivera",
                            "phone": "+15551234567",
                            "start_time": "2026-10-28T14:00:00",
                        },
                    },
                }
            ],
            "call": {"id": "call_replay_01"},
        }
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # First booking call
        resp1 = await client.post("/webhook/vapi", json=payload, headers=headers)
        assert resp1.status_code == 200
        assert events_created == 1

        # Replayed booking call with same call_id and slot
        resp2 = await client.post("/webhook/vapi", json=payload, headers=headers)
        assert resp2.status_code == 200
        assert resp1.json() == resp2.json()
        # Events API must NOT be called a second time
        assert events_created == 1


async def test_webhook_dnc_and_transfer_tools(
    integration_app: FastAPI,
    test_settings: Settings,
) -> None:
    """Verify statutory DNC and warm transfer tool dispatches."""
    dummy_gcal = make_gcal_transport(lambda req: httpx.Response(200, json={}))
    calendar_client = GoogleCalendarClient.from_settings(dummy_gcal, test_settings)
    integration_app.state.booking_coordinator = BookingCoordinator(
        calendar_client, test_settings
    )

    transport = httpx.ASGITransport(app=integration_app)
    headers = {"X-Vapi-Secret": "test-vapi-secret"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. DNC request
        payload_dnc = {
            "message": {
                "type": "tool-calls",
                "toolCalls": [
                    {
                        "id": "dnc_01",
                        "type": "function",
                        "function": {
                            "name": "record_dnc_request",
                            "arguments": {"phone": "+15559990000"},
                        },
                    }
                ],
                "call": {"id": "call_dnc"},
            }
        }
        resp_dnc = await client.post("/webhook/vapi", json=payload_dnc, headers=headers)
        assert resp_dnc.status_code == 200
        assert "removed this number" in resp_dnc.json()["results"][0]["result"]

        # 2. Transfer request
        payload_transfer = {
            "message": {
                "type": "tool-calls",
                "toolCalls": [
                    {
                        "id": "transfer_01",
                        "type": "function",
                        "function": {
                            "name": "transfer_call",
                            "arguments": {"reason": "Caller upset"},
                        },
                    }
                ],
                "call": {"id": "call_trans"},
            }
        }
        resp_trans = await client.post(
            "/webhook/vapi", json=payload_transfer, headers=headers
        )
        assert resp_trans.status_code == 200
        assert "transfer you directly" in resp_trans.json()["results"][0]["result"]


async def test_webhook_malformed_arguments_safe_envelope(
    integration_app: FastAPI,
    test_settings: Settings,
) -> None:
    """Verify tool argument validation failure returns friendly voice fallback."""
    dummy_gcal = make_gcal_transport(lambda req: httpx.Response(200, json={}))
    calendar_client = GoogleCalendarClient.from_settings(dummy_gcal, test_settings)
    integration_app.state.booking_coordinator = BookingCoordinator(
        calendar_client, test_settings
    )

    transport = httpx.ASGITransport(app=integration_app)
    headers = {"X-Vapi-Secret": "test-vapi-secret"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Missing required customer_name and phone
        payload = {
            "message": {
                "type": "tool-calls",
                "toolCalls": [
                    {
                        "id": "bad_args_tool",
                        "type": "function",
                        "function": {
                            "name": "book_appointment",
                            "arguments": {"start_time": "2026-10-28T14:00:00"},
                        },
                    }
                ],
                "call": {"id": "call_bad_args"},
            }
        }
        response = await client.post("/webhook/vapi", json=payload, headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["toolCallId"] == "bad_args_tool"
        assert (
            "trouble processing those booking details" in data["results"][0]["result"]
        )
