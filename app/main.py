"""FastAPI application factory, lifespan management, and server entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.webhook import router as webhook_router
from app.config import get_settings
from app.errors import WebhookAuthError
from app.logging import configure_logging, get_logger

logger = get_logger("app_main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown resource lifecycles."""
    configure_logging()
    settings = get_settings()
    logger.info(
        "starting_voice_agent_service",
        environment=settings.environment,
        clinic_name=settings.clinic_name,
        clinic_timezone=settings.clinic_timezone,
    )

    async with httpx.AsyncClient(follow_redirects=False) as client:
        app.state.http_client = client
        yield

    logger.info("shutting_down_voice_agent_service")


def create_app() -> FastAPI:
    """Instantiate and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="voice-ai-appointment-agent",
        description="24/7 Voice AI Receptionist with Google Calendar Integration",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS configuration (agent-security.md §8)
    allowed_origins = (
        ["*"] if settings.environment in ("development", "test") else []
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(webhook_router)

    @app.exception_handler(WebhookAuthError)
    async def webhook_auth_exception_handler(
        _request: Request, exc: WebhookAuthError
    ) -> JSONResponse:
        logger.warning("webhook_auth_failed", error=str(exc))
        return JSONResponse(
            status_code=401,
            content={
                "detail": "Unauthorized: invalid or missing webhook secret"
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        # Unmapped tool names fail closed to friendly voice error
        is_unmapped_tool = any(
            err.get("loc", ())[-1] == "name"
            and "enum" in str(err.get("type", ""))
            for err in errors
        )
        if is_unmapped_tool:
            logger.error("unmapped_tool_called", errors=errors)
            tool_call_id = "unknown"
            try:
                raw_body = await request.json()
                calls = (
                    raw_body.get("message", {}).get("toolCalls")
                    or raw_body.get("message", {}).get("toolCallList")
                    or []
                )
                if (
                    calls
                    and isinstance(calls, list)
                    and isinstance(calls[0], dict)
                ):
                    tool_call_id = str(calls[0].get("id", "unknown"))
            except Exception as read_exc:
                logger.debug(
                    "failed_extracting_tool_call_id", error=str(read_exc)
                )

            return JSONResponse(
                status_code=200,
                content={
                    "results": [
                        {
                            "toolCallId": tool_call_id,
                            "result": (
                                "I am only able to help with checking availability "
                                "and booking appointments."
                            ),
                        }
                    ]
                },
            )

        logger.warning("request_validation_failed", errors=errors)
        return JSONResponse(
            status_code=422,
            content={"detail": exc.errors()},
        )

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        """Health check endpoint for Docker container checks and uptime probes."""
        return {"status": "ok", "service": "voice-ai-appointment-agent"}

    return app


app = create_app()


def run() -> None:
    """CLI entrypoint for [project.scripts] 'voice-agent'."""
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        # Required for containerized deployment ingress
        host="0.0.0.0",  # noqa: S104
        port=8000,
        log_level=settings.log_level.lower(),
        reload=settings.environment == "development",
    )


if __name__ == "__main__":
    run()
