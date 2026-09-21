"""Application entrypoint and wiring.

    uvicorn app.main:app --reload

Composition root: the singletons are built here, in the lifespan, and attached
to ``app.state``. Routes reach them only through :mod:`app.api.deps`, which
keeps the dependency direction one-way -- routes depend on services, services
never import routes -- and lets tests build a fresh app with its own state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import calls, frontend, health, webhooks
from app.config import Settings, get_settings
from app.services.broadcaster import Broadcaster
from app.services.call_registry import CallRegistry
from app.services.reconcile import reconcile_hold_queue
from app.telephony.provider_client import create_telephony_client

STATIC_DIR = Path(__file__).parent / "static"


def configure_logging(level: str) -> None:
    """Apply LOG_LEVEL to this application only.

    The root logger stays at WARNING deliberately. Setting it to the
    configured level would make LOG_LEVEL=DEBUG unusable -- asyncio, uvicorn
    and httpx would bury our own lines in per-socket chatter.
    """
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("app").setLevel(level.upper())
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(settings.log_level)
    log = logging.getLogger(__name__)

    app.state.telephony = create_telephony_client(settings)

    broadcaster = Broadcaster(queue_max=settings.frontend_queue_max)
    app.state.broadcaster = broadcaster
    app.state.registry = CallRegistry(
        broadcaster=broadcaster,
        history_size=settings.call_history_size,
    )

    log.info(
        "call screener up env=%s public=%s",
        settings.app_env,
        settings.public_base_url,
    )
    if app.state.telephony.is_placeholder:
        log.warning(
            "telephony REST client is a PLACEHOLDER -- accept/reject will not "
            "actually control calls at the carrier"
        )
    if settings.app_env != "local" and not settings.validate_webhook_signature:
        log.warning(
            "VALIDATE_WEBHOOK_SIGNATURE is off outside local -- the call webhook is spoofable"
        )

    # Deliberately a background task, not an await. Uvicorn does not accept
    # connections until lifespan startup returns, so blocking here on a slow
    # Twilio would make the number refuse *new* calls in order to recover old
    # ones -- exactly the wrong trade. The queue fills in a moment later and
    # every dashboard is told over the socket.
    reconciler: asyncio.Task[int] | None = None
    if settings.reconcile_on_startup:
        reconciler = asyncio.create_task(
            reconcile_hold_queue(app.state.registry, settings), name="reconcile-hold-queue"
        )

    try:
        yield
    finally:
        if reconciler is not None and not reconciler.done():
            reconciler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reconciler
        # Release dashboards before the server stops accepting, so clients see
        # a clean close and reconnect rather than a timeout.
        broadcaster.close_all()
        log.info("call screener shutting down")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Call Screener API",
        version="0.1.0",
        summary="Telephony webhooks, streaming transcription, and dashboard fan-out",
        lifespan=lifespan,
    )

    # Only needed when the dashboard is served from a different origin. The
    # compose setup proxies through Vite, so in dev this is a no-op.
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(calls.router)
    app.include_router(frontend.router)

    # Serves the greeting MP3 when GREETING_AUDIO_URL is unset. Fine for local
    # work; in production put the audio on a CDN so the provider fetches it
    # from somewhere that is not your API.
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


app = create_app()
