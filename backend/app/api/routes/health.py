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
    """Ready means "safe to send traffic to"."""
    return {"status": "ok", "signatureChecks": settings.validate_webhook_signature}


@router.get("/api/stats", summary="Live counters for debugging")
async def stats(registry: RegistryDep, broadcaster: BroadcasterDep) -> dict[str, int]:
    return {
        "openCalls": len(registry.open_calls()),
        "dashboardClients": broadcaster.subscriber_count,
    }
