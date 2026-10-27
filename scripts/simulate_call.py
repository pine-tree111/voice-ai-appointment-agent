"""Interactive CLI caller simulator for voice-ai-appointment-agent."""

import argparse
import sys
import time
import uuid
from collections.abc import Sequence
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.logging import configure_logging, get_logger

logger = get_logger("simulate_call")

FORBIDDEN_ACOUSTIC_TOKENS: tuple[str, ...] = (
    "**",
    "*",
    "#",
    "- ",
    "http://",
    "https://",
    "[",
    "]",
    "{",
    "}",
)


def verify_acoustic_formatting(spoken_text: str) -> list[str]:
    """Verify that spoken output adheres to acoustic anti-markdown invariants.

    Returns a list of detected violations, if any.
    """
    violations: list[str] = []
    for token in FORBIDDEN_ACOUSTIC_TOKENS:
        if token in spoken_text:
            violations.append(f"Contains prohibited acoustic token: '{token}'")
    return violations


def build_tool_call_payload(
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str,
    caller_phone: str,
    caller_name: str,
) -> dict[str, Any]:
    """Construct a VapiWebhookPayload dictionary."""
    tool_call_id = f"call_{uuid.uuid4().hex[:8]}"
    return {
        "message": {
            "type": "tool-calls",
            "toolCalls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": arguments,
                    },
                }
            ],
            "call": {
                "id": call_id,
                "customer": {
                    "number": caller_phone,
                    "name": caller_name,
                },
            },
        }
    }


def execute_turn(
    url: str,
    secret: str,
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str,
    caller_phone: str,
    caller_name: str,
    timeout: float = 5.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Dispatch a simulated turn to the webhook and measure latency."""
    payload = build_tool_call_payload(
        tool_name=tool_name,
        arguments=arguments,
        call_id=call_id,
        caller_phone=caller_phone,
        caller_name=caller_name,
    )
    headers = {
        "X-Vapi-Secret": secret,
        "Content-Type": "application/json",
    }

    start_time = time.perf_counter()
    if client is not None:
        response = client.post(url, json=payload, headers=headers, timeout=timeout)
    else:
        with httpx.Client() as c:
            response = c.post(url, json=payload, headers=headers, timeout=timeout)
    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    if response.status_code != 200:
        logger.error(
            "webhook_invocation_failed",
            status_code=response.status_code,
            body=response.text,
            elapsed_ms=round(elapsed_ms, 2),
        )
        response.raise_for_status()

    result_json: Any = response.json()
    if not isinstance(result_json, dict):
        raise ValueError("Invalid webhook response format: expected JSON object")

    results: list[dict[str, Any]] = result_json.get("results", [])
    spoken_text = results[0].get("result", "") if results else ""

    violations = verify_acoustic_formatting(spoken_text)
    if violations:
        logger.warning(
            "acoustic_invariant_violation",
            violations=violations,
            spoken_text=spoken_text,
        )

    logger.info(
        "simulated_turn_success",
        tool=tool_name,
        elapsed_ms=round(elapsed_ms, 2),
        latency_budget_exceeded=elapsed_ms > 400.0,
        spoken_response=spoken_text,
    )

    return {
        "elapsed_ms": elapsed_ms,
        "spoken_text": spoken_text,
        "violations": violations,
        "raw_response": result_json,
    }


def run_scenario(
    scenario_name: str,
    url: str,
    secret: str,
    call_id: str,
    phone: str,
    name: str,
    client: httpx.Client | None = None,
) -> int:
    """Execute a multi-turn scenario."""
    logger.info("starting_call_simulation", scenario=scenario_name, call_id=call_id)

    try:
        if scenario_name == "happy_path":
            # Turn 1: Check availability
            turn1 = execute_turn(
                url=url,
                secret=secret,
                tool_name="check_availability",
                arguments={"start_time": "2026-10-28"},
                call_id=call_id,
                caller_phone=phone,
                caller_name=name,
                client=client,
            )
            # Turn 2: Book appointment
            turn2 = execute_turn(
                url=url,
                secret=secret,
                tool_name="book_appointment",
                arguments={
                    "customer_name": name,
                    "phone": phone,
                    "start_time": "2026-10-28T14:00:00",
                    "service_type": "General Consultation",
                },
                call_id=call_id,
                caller_phone=phone,
                caller_name=name,
                client=client,
            )
            logger.info(
                "scenario_completed",
                total_turns=2,
                turn1_latency_ms=round(turn1["elapsed_ms"], 2),
                turn2_latency_ms=round(turn2["elapsed_ms"], 2),
            )
            return 0

        elif scenario_name == "dnc":
            turn = execute_turn(
                url=url,
                secret=secret,
                tool_name="record_dnc_request",
                arguments={"phone": phone, "reason": "Caller requested removal"},
                call_id=call_id,
                caller_phone=phone,
                caller_name=name,
                client=client,
            )
            logger.info(
                "dnc_scenario_completed",
                latency_ms=round(turn["elapsed_ms"], 2),
            )
            return 0

        elif scenario_name == "transfer":
            turn = execute_turn(
                url=url,
                secret=secret,
                tool_name="transfer_call",
                arguments={
                    "reason": "Customer requested human receptionist",
                    "department": "front_desk",
                },
                call_id=call_id,
                caller_phone=phone,
                caller_name=name,
                client=client,
            )
            logger.info(
                "transfer_scenario_completed", latency_ms=round(turn["elapsed_ms"], 2)
            )
            return 0

        else:
            logger.error("unknown_scenario", scenario=scenario_name)
            return 1

    except Exception as e:
        logger.error("scenario_failed", error=str(e))
        return 1


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Simulate inbound phone caller conversational tool webhooks."
    )
    parser.add_argument(
        "--url",
        type=str,
        default="http://localhost:8000/webhook/vapi",
        help="FastAPI Vapi webhook target URL",
    )
    parser.add_argument(
        "--scenario",
        type=str,
        default="happy_path",
        choices=["happy_path", "dnc", "transfer"],
        help="Predefined scenario to execute",
    )
    parser.add_argument(
        "--call-id",
        type=str,
        default=None,
        help="Custom call ID (defaults to auto-generated UUID)",
    )
    parser.add_argument(
        "--phone",
        type=str,
        default="+15551234567",
        help="Caller phone number",
    )
    parser.add_argument(
        "--name",
        type=str,
        default="Alex Rivera",
        help="Caller full name",
    )
    return parser.parse_args(args)


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint for [project.scripts] 'simulate-call'."""
    configure_logging()
    settings: Settings = get_settings()
    args = parse_args(argv)

    call_id = args.call_id or f"sim_{uuid.uuid4().hex[:10]}"
    secret = settings.vapi_secret_token.get_secret_value()

    exit_code = run_scenario(
        scenario_name=args.scenario,
        url=args.url,
        secret=secret,
        call_id=call_id,
        phone=args.phone,
        name=args.name,
    )
    if exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
