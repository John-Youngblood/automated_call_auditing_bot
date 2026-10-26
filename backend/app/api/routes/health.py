"""Liveness, readiness, and a little operational visibility."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import BroadcasterDep, RegistryDep, SettingsDep, StoreDep

router = APIRouter(tags=["ops"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe")
async def readyz(settings: SettingsDep, store: StoreDep) -> dict[str, object]:
    """Ready means "safe to send traffic to".

    Storage failing does not change that -- calls are still screened -- so it
    is reported alongside rather than as a failed probe.
    """
    return {
        "status": "ok",
        "signatureChecks": settings.validate_webhook_signature,
        "storage": store.warning or "ok",
    }


@router.get("/api/stats", summary="Live counters for debugging")
async def stats(registry: RegistryDep, broadcaster: BroadcasterDep) -> dict[str, int]:
    return {
        "openCalls": len(registry.open_calls()),
        "dashboardClients": broadcaster.subscriber_count,
    }
