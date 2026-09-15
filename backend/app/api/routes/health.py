"""Liveness, readiness, and a little operational visibility."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import BroadcasterDep, RegistryDep, SettingsDep

router = APIRouter(tags=["ops"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe")
async def readyz(settings: SettingsDep) -> dict[str, object]:
    """Ready means "safe to send traffic to".

    Deliberately does not fail on a missing Deepgram key: transcription
    degrades, but the service can still answer calls, and refusing traffic
    would drop calls entirely.
    """
    return {
        "status": "ok",
        "provider": settings.telephony_provider,
        "sttMode": "deepgram" if settings.stt_enabled else "mock",
    }


@router.get("/api/stats", summary="Live counters for debugging")
async def stats(registry: RegistryDep, broadcaster: BroadcasterDep) -> dict[str, int]:
    return {
        "openCalls": len(registry.open_calls()),
        "dashboardClients": broadcaster.subscriber_count,
    }
