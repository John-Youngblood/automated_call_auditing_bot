"""Named callers and favourites.

    POST   /api/contacts            create or update (name and/or star)
    GET    /api/contacts            list, favourites first
    DELETE /api/contacts/{number}   forget

One endpoint covers naming and starring because they are the same act: putting
your own label on a number. Partial updates are the norm -- the star button
sends only ``isFavorite``, the name dialog sends only ``displayName`` -- so
omitted fields are left alone rather than cleared.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.api.deps import ContactsDep, RegistryDep
from app.schemas.contacts import ContactOut, ContactUpsertRequest
from app.services.phone import InvalidPhoneNumber

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/contacts", tags=["contacts"])


@router.post(
    "",
    summary="Name a caller and/or star them",
    responses={400: {"description": "Not a usable phone number"}},
)
async def upsert_contact(
    payload: ContactUpsertRequest,
    contacts: ContactsDep,
    registry: RegistryDep,
) -> ContactOut:
    try:
        contact = await contacts.upsert(
            payload.number,
            display_name=payload.display_name,
            is_favorite=payload.is_favorite,
        )
    except InvalidPhoneNumber as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    # Calls already on the dashboard were resolved when they arrived, so they
    # are now stale. Refresh them in place rather than making the client
    # cross-reference contacts on every render.
    await _refresh_live_calls(contact, registry)
    return contact


@router.get("", summary="All named and starred callers")
async def list_contacts(contacts: ContactsDep) -> list[ContactOut]:
    return await contacts.list_all()


@router.delete(
    "/{number}",
    summary="Forget a contact",
    responses={404: {"description": "No such contact"}},
)
async def delete_contact(
    number: str,
    contacts: ContactsDep,
    registry: RegistryDep,
) -> dict[str, str]:
    try:
        removed = await contacts.delete(number)
    except InvalidPhoneNumber as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no contact for that number")

    await _refresh_live_calls(None, registry, number=number)
    return {"status": "ok"}


async def _refresh_live_calls(
    contact: ContactOut | None,
    registry: RegistryDep,
    number: str | None = None,
) -> None:
    """Re-resolve name and star on any of this caller's calls in flight."""
    target = contact.number if contact is not None else number
    if not target:
        return

    for call in registry.find_by_number(target):
        registry.update_caller(
            call.call_id,
            name=contact.display_name if contact else None,
            is_favorite=contact.is_favorite if contact else False,
        )
