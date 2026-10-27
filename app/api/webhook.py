"""FastAPI router for Vapi telephony webhooks and tool dispatch."""

import hmac
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, Request
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.errors import VoiceAgentError, WebhookAuthError
from app.logging import get_logger, sanitize_log_string
from app.models.appointment import (
    BookAppointmentInput,
    CheckAvailabilityInput,
    DNCRequestInput,
    TransferCallInput,
)
from app.models.vapi import (
    ToolName,
    VapiToolResultItem,
    VapiWebhookPayload,
    VapiWebhookResponse,
)
from app.services.booking import BookingCoordinator
from app.services.calendar import GoogleCalendarClient

logger = get_logger("webhook_router")

router = APIRouter(prefix="/webhook", tags=["telephony"])


async def verify_vapi_secret(
    settings: Annotated[Settings, Depends(get_settings)],
    x_vapi_secret: Annotated[str | None, Header(alias="X-Vapi-Secret")] = None,
) -> None:
    """Validate X-Vapi-Secret header using constant-time comparison.

    Missing or invalid secret tokens immediately
    raise WebhookAuthError (returning HTTP 401 Unauthorized).
    """
    if not x_vapi_secret:
        logger.warning("missing_vapi_secret_header")
        raise WebhookAuthError("Missing X-Vapi-Secret header")

    expected_secret = settings.vapi_secret_token.get_secret_value()
    if not hmac.compare_digest(
        x_vapi_secret.encode("utf-8"),
        expected_secret.encode("utf-8"),
    ):
        logger.warning("invalid_vapi_secret_rejected")
        raise WebhookAuthError("Invalid X-Vapi-Secret header")


def get_booking_coordinator(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> BookingCoordinator:
    """Retrieve or initialize singleton BookingCoordinator for requests."""
    coordinator = getattr(request.app.state, "booking_coordinator", None)
    if isinstance(coordinator, BookingCoordinator):
        return coordinator

    raw_client = getattr(request.app.state, "http_client", None)
    client = (
        raw_client if isinstance(raw_client, httpx.AsyncClient) else httpx.AsyncClient()
    )

    calendar_client = GoogleCalendarClient.from_settings(client, settings)
    new_coordinator = BookingCoordinator(calendar_client, settings)
    request.app.state.booking_coordinator = new_coordinator
    return new_coordinator


@router.post("/vapi", response_model=VapiWebhookResponse)
async def handle_vapi_webhook(
    payload: VapiWebhookPayload,
    _auth: Annotated[None, Depends(verify_vapi_secret)],
    coordinator: Annotated[BookingCoordinator, Depends(get_booking_coordinator)],
) -> VapiWebhookResponse:
    """Dispatch Vapi tool calls and return conversational speech responses.

    Lifecycle Invariant: If payload.message.type != 'tool-calls' (e.g.
    'status-update' or 'end-of-call-report'), logs cleanly and returns results=[].
    """
    msg_type = payload.message.type
    call_id = (
        payload.message.call.id
        if payload.message.call is not None
        else "call_anonymous"
    )
    clean_call_id = sanitize_log_string(call_id)

    if msg_type != "tool-calls":
        logger.info(
            "vapi_lifecycle_event_ignored",
            event_type=msg_type,
            call_id=clean_call_id,
        )
        return VapiWebhookResponse(results=[])

    tool_calls = payload.message.tool_calls
    logger.info(
        "processing_vapi_tool_calls",
        call_id=clean_call_id,
        count=len(tool_calls),
    )

    results: list[VapiToolResultItem] = []

    for tool_call in tool_calls:
        tool_id = tool_call.id
        func_call = tool_call.function
        tool_name = func_call.name
        args_dict = func_call.arguments

        result_text: str

        if tool_name == ToolName.CHECK_AVAILABILITY:
            try:
                avail_args = CheckAvailabilityInput.model_validate(args_dict)
                result_text = await coordinator.handle_check_availability(
                    avail_args, call_id=clean_call_id
                )
            except ValidationError as exc:
                logger.warning(
                    "check_availability_arg_error",
                    call_id=clean_call_id,
                    error=str(exc),
                )
                result_text = (
                    "I had trouble understanding the date you requested. "
                    "Could you please repeat that?"
                )
            except VoiceAgentError as exc:
                logger.error(
                    "check_availability_domain_error",
                    call_id=clean_call_id,
                    error=str(exc),
                )
                result_text = (
                    "My scheduling system is taking a moment. "
                    "May I take your preferred time?"
                )

        elif tool_name == ToolName.BOOK_APPOINTMENT:
            try:
                book_args = BookAppointmentInput.model_validate(args_dict)
                result_text = await coordinator.handle_book_appointment(
                    book_args, call_id=clean_call_id
                )
            except ValidationError as exc:
                logger.warning(
                    "book_appointment_arg_error",
                    call_id=clean_call_id,
                    error=str(exc),
                )
                result_text = (
                    "I had trouble processing those booking details. "
                    "Could you please repeat that for me?"
                )
            except VoiceAgentError as exc:
                logger.error(
                    "book_appointment_domain_error",
                    call_id=clean_call_id,
                    error=str(exc),
                )
                result_text = (
                    "My scheduling system is taking a moment. "
                    "May I take your preferred time?"
                )

        elif tool_name == ToolName.RECORD_DNC_REQUEST:
            try:
                dnc_args = DNCRequestInput.model_validate(args_dict)
                result_text = await coordinator.handle_dnc_request(
                    dnc_args, call_id=clean_call_id
                )
            except ValidationError:
                result_text = (
                    "Understood. I have removed this number from our list. "
                    "Have a great day."
                )

        elif tool_name == ToolName.TRANSFER_CALL:
            try:
                transfer_args = TransferCallInput.model_validate(args_dict)
                result_text = await coordinator.handle_transfer_call(
                    transfer_args, call_id=clean_call_id
                )
            except ValidationError:
                result_text = (
                    "One moment while I transfer you directly to our front desk team."
                )

        results.append(
            VapiToolResultItem(
                tool_call_id=tool_id,
                result=result_text,
            )
        )

    return VapiWebhookResponse(results=results)
