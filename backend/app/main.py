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

from app.api.routes import calls, frontend, health, session, webhooks
from app.config import Settings, get_settings
from app.services.broadcaster import Broadcaster
from app.services.call_registry import CallRegistry
from app.services.line_state import LineState
from app.services.reconcile import reconcile_hold_queue
from app.services.sessions import MIN_PASSWORD_LENGTH, Sessions
from app.telephony.rest import client_from_settings

STATIC_DIR = Path(__file__).parent / "static"

#: The built dashboard, when it has been baked into the image. Present in the
#: Cloud Run image (see the repo-root Dockerfile), absent everywhere else --
#: dev serves it from Vite and the office stack from nginx.
DASHBOARD_DIR = Path(__file__).parent / "dashboard"

#: The shipped default for HOST_PHONE_NUMBER. Reaching production with this
#: still set is a configuration failure, not a preference.
PLACEHOLDER_HOST_NUMBER = "+15550000000"


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

    broadcaster = Broadcaster(queue_max=settings.frontend_queue_max)
    app.state.broadcaster = broadcaster
    app.state.registry = CallRegistry(
        broadcaster=broadcaster,
        history_size=settings.call_history_size,
    )
    app.state.line = LineState(broadcaster, is_open=settings.line_open_on_start)
    app.state.sessions = Sessions(settings.dashboard_password)

    log.info(
        "call screener up env=%s public=%s line=%s",
        settings.app_env,
        settings.public_base_url,
        "open" if app.state.line.is_open else "CLOSED",
    )
    if client_from_settings(settings) is None:
        log.warning(
            "no Twilio REST credentials -- accept, reject and closing the line "
            "will fail until TWILIO_ACCOUNT_SID and an API key are set"
        )
    if settings.app_env != "local" and not settings.validate_webhook_signature:
        log.warning(
            "VALIDATE_WEBHOOK_SIGNATURE is off outside local -- the call webhook is spoofable"
        )

    # An open dashboard on a public URL means anyone who finds it can read
    # every caller's transcript and take the show off air.
    if settings.app_env != "local":
        if not settings.dashboard_password:
            raise RuntimeError(
                "DASHBOARD_PASSWORD is not set. The dashboard and the API would "
                "be open to anyone who finds the URL."
            )
        if len(settings.dashboard_password) < MIN_PASSWORD_LENGTH:
            raise RuntimeError(
                f"DASHBOARD_PASSWORD is shorter than {MIN_PASSWORD_LENGTH} characters. "
                "It is a shared secret on a public URL with no rate limiting."
            )
    elif not settings.dashboard_password:
        log.warning("DASHBOARD_PASSWORD is not set -- the dashboard is open")

    # A misconfigured host number is silent in the worst way: Accept succeeds,
    # the dashboard says the caller is on air, and Twilio dials a number that
    # goes nowhere. Refuse to start rather than discover it mid-show.
    if settings.host_phone_number == PLACEHOLDER_HOST_NUMBER:
        message = (
            f"HOST_PHONE_NUMBER is still the placeholder {PLACEHOLDER_HOST_NUMBER}. "
            "Accepted callers would be dialled to nowhere while the dashboard "
            "showed them on air."
        )
        if settings.app_env == "local":
            log.warning("%s Fine locally; this refuses to start anywhere else.", message)
        else:
            raise RuntimeError(message)

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

    # No `version=`: the dashboard footer is the one place a version is stated,
    # and it comes from frontend/package.json at build time. A second copy here
    # would only ever be the one somebody forgot to bump. FastAPI falls back to
    # its own default for the OpenAPI spec, which nothing depends on.
    app = FastAPI(
        title="Call Screener API",
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
    app.include_router(session.router)
    app.include_router(calls.router)
    app.include_router(frontend.router)

    # Serves the greeting MP3 when GREETING_AUDIO_URL is unset. Fine for local
    # work; in production put the audio on a CDN so the provider fetches it
    # from somewhere that is not your API.
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Last, because "/" matches every path: the routers above are registered
    # first and so win. Serving the dashboard from the API means one origin,
    # which is why the client's VITE_* overrides can stay unset -- see
    # frontend/src/lib/api.ts. `html=True` serves index.html for "/".
    if DASHBOARD_DIR.is_dir():
        app.mount("/", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")

    return app


app = create_app()
