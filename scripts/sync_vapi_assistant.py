"""Synchronize and validate vapi_assistant.json against the Vapi REST API."""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.errors import SecurityViolationError
from app.logging import configure_logging, get_logger

logger = get_logger("sync_vapi_assistant")

MAX_SCHEMA_BYTES: int = 1_048_576  # 1 MiB hard ceiling


def _toctou_safe_read(resolved_path: Path, max_bytes: int = MAX_SCHEMA_BYTES) -> str:
    """Read a local file using TOCTOU-safe descriptor checks.

    Enforces O_NOFOLLOW to block symlink swaps and caps bytes read to max_bytes.
    """
    fd = os.open(str(resolved_path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        stat = os.fstat(fd)
        if stat.st_size > max_bytes:
            raise SecurityViolationError(
                f"Assistant schema file exceeds inspection ceiling of {max_bytes} bytes"
            )
        raw_bytes = os.read(fd, stat.st_size)
        return raw_bytes.decode("utf-8")
    finally:
        os.close(fd)


def load_assistant_schema(file_path: Path, project_root: Path) -> dict[str, Any]:
    """Validate project containment, read safely, and parse JSON schema."""
    resolved = file_path.resolve(strict=True)
    if not resolved.is_relative_to(project_root):
        raise SecurityViolationError(
            f"Path {resolved} escapes project root boundary {project_root}"
        )

    content = _toctou_safe_read(resolved)
    data: Any = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("Assistant schema must be a top-level JSON object")
    return data


def sync_assistant(
    schema: dict[str, Any],
    api_key: str,
    assistant_id: str | None = None,
    create: bool = False,
    base_url: str = "https://api.vapi.ai",
    timeout: float = 30.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Send assistant schema payload to Vapi API (POST if create, PATCH if updating)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    should_create = create or not assistant_id
    if should_create:
        endpoint = f"{base_url}/assistant"
    else:
        endpoint = f"{base_url}/assistant/{assistant_id}"
    method = "POST" if should_create else "PATCH"

    logger.info(
        "syncing_assistant_schema",
        method=method,
        endpoint=endpoint,
        tools_count=len(schema.get("model", {}).get("tools", [])),
    )

    def _execute(c: httpx.Client) -> httpx.Response:
        return c.request(
            method=method,
            url=endpoint,
            headers=headers,
            json=schema,
            timeout=timeout,
        )

    if client is not None:
        response = _execute(client)
    else:
        with httpx.Client() as c:
            response = _execute(c)

    if response.status_code not in (200, 201):
        logger.error(
            "vapi_sync_failed",
            status_code=response.status_code,
            response_body=response.text,
        )
        response.raise_for_status()

    result: Any = response.json()
    if not isinstance(result, dict):
        return {"response": result}
    return result


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command line flags."""
    parser = argparse.ArgumentParser(
        description="Validate and synchronize vapi_assistant.json to Vapi REST API."
    )
    parser.add_argument(
        "--file",
        "-f",
        type=Path,
        default=None,
        help=(
            "Path to assistant JSON schema (defaults to "
            "vapi_assistant.json in project root)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate JSON schema locally without making outbound network requests",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="Force creation of a new assistant instead of updating the existing ID",
    )
    parser.add_argument(
        "--assistant-id",
        type=str,
        default=None,
        help=(
            "Override target assistant ID (defaults to "
            "VAPI_ASSISTANT_ID from settings)"
        ),
    )
    return parser.parse_args(args)


def run_sync(
    args: argparse.Namespace,
    settings: Settings,
    client: httpx.Client | None = None,
) -> int:
    """Execute validation and conditional synchronization."""
    target_path = args.file or (settings.project_root / "vapi_assistant.json")

    try:
        schema = load_assistant_schema(target_path, settings.project_root)
    except Exception as e:
        logger.error("schema_load_error", error=str(e), path=str(target_path))
        return 1

    assistant_name = schema.get("name", "Unknown")
    tools = schema.get("model", {}).get("tools", [])
    logger.info(
        "schema_validated_successfully",
        name=assistant_name,
        model=schema.get("model", {}).get("model"),
        tools_registered=[t.get("function", {}).get("name") for t in tools],
        first_message_preview=schema.get("firstMessage", "")[:50] + "...",
    )

    if args.dry_run:
        logger.info("dry_run_complete_skipping_network_dispatch")
        return 0

    api_key = settings.vapi_api_key.get_secret_value()
    if not api_key or api_key == "mock-vapi-key":
        logger.error(
            "missing_or_mock_api_key",
            detail=(
                "Valid VAPI_API_KEY is required for live synchronization. "
                "Use --dry-run for offline verification."
            ),
        )
        return 1

    target_id = args.assistant_id or settings.vapi_assistant_id

    try:
        result = sync_assistant(
            schema=schema,
            api_key=api_key,
            assistant_id=target_id,
            create=args.create,
            client=client,
        )
        synced_id = result.get("id", target_id)
        logger.info("assistant_synced_successfully", assistant_id=synced_id)
        return 0
    except Exception as e:
        logger.error("sync_execution_failed", error=str(e))
        return 1


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint for [project.scripts] 'sync-assistant'."""
    configure_logging()
    settings = get_settings()
    args = parse_args(argv)
    exit_code = run_sync(args, settings)
    if exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
