"""Contacts: a name, a note and a standing for a phone number.

    GET   /api/contacts
    PATCH /api/contacts/{number}

One row per number, kept across restarts. A favorite gets a star and nothing
else. A blocked number is turned away before it is answered (see
app/api/routes/webhooks.py), and blocking one ends any call it has waiting --
Twilio first, as with Reject.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import Field, StringConstraints, field_validator

from app.api.auth import RequireSession
from app.api.deps import RegistryDep, SettingsDep, StoreDep
from app.schemas.calls import (
    CallStatus,
    CamelModel,
    Contact,
    ContactListItem,
    ContactStanding,
    utc_now,
)
from app.schemas.events import ServerEvent
from app.services.decisions import TelephonyUnavailable, turn_away
from app.services.phone import InvalidPhoneNumber, parse_typed_number

router = APIRouter(prefix="/api/contacts", tags=["contacts"], dependencies=[RequireSession])


class ContactPatch(CamelModel):
    """The fields to change. One that is omitted is left alone; null clears it."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] | None = None
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    standing: ContactStanding | None = None

    @field_validator("name", "note")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        # An emptied text box means "no name", not a name of "".
        return value or None


class ContactChangeResult(CamelModel):
    """The contact as saved (None once removed), and what blocking did.

    ``failed_call_ids`` matters as it does for closing the line: those callers
    are still holding, so silence must not suggest otherwise.
    """

    contact: Contact | None
    #: Calls from the number that blocking hung up on.
    ended_call_ids: list[str] = Field(default_factory=list)
    #: Calls we could not reach. They keep their place on the dashboard.
    failed_call_ids: list[str] = Field(default_factory=list)


@router.get("", summary="Every contact, favorites first")
async def list_contacts(store: StoreDep) -> list[ContactListItem]:
    return store.contacts()


@router.patch("/{number}", summary="Name, note, favorite or block a number")
async def change_contact(
    number: str,
    patch: ContactPatch,
    store: StoreDep,
    registry: RegistryDep,
    settings: SettingsDep,
) -> ContactChangeResult:
    """Change only the fields sent. Clearing every field removes the contact.

    Blocking also turns away any call from the number that is being screened
    or holding -- never one on air: ending a live conversation is the host's
    decision, not the screener's.
    """
    try:
        number = parse_typed_number(number)
    except InvalidPhoneNumber as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    current = store.get_contact(number)
    contact = _merge(number, current, patch)
    saved = store.put_contact(contact) if contact else store.delete_contact(number)
    if not saved:
        registry.announce_storage_warning()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Couldn't save the contact")

    registry.apply_contact(number, contact)
    registry.publish(ServerEvent.contact_changed(number, contact))

    result = ContactChangeResult(contact=contact)
    newly_blocked = (
        contact is not None
        and contact.standing == "blocked"
        and (current is None or current.standing != "blocked")
    )
    if not newly_blocked:
        return result

    # Twilio first, as with Reject: a caller we failed to reach stays in the
    # queue rather than vanishing while still connected.
    for call in [c for c in registry.open_calls() if c.caller.number == number]:
        try:
            declined = await turn_away(call.call_id, settings)
        except TelephonyUnavailable:
            declined = False
        if declined:
            registry.set_status(call.call_id, CallStatus.REJECTED)
            result.ended_call_ids.append(call.call_id)
        else:
            result.failed_call_ids.append(call.call_id)
    return result


def _merge(number: str, current: Contact | None, patch: ContactPatch) -> Contact | None:
    """``current`` with the fields ``patch`` sent, or None if nothing is left."""
    fields = (
        current.model_dump(include={"name", "note", "standing"})
        if current
        else {"name": None, "note": None, "standing": None}
    )
    fields.update(patch.model_dump(include=patch.model_fields_set))
    if not any(fields.values()):
        return None

    now = utc_now()
    if current and fields["standing"] == current.standing:
        since, attempts = current.standing_since, current.blocked_attempts
    elif fields["standing"] is None:
        since, attempts = None, 0
    else:
        # A new standing starts its own clock and its own count of refused rings.
        since, attempts = now, 0
    return Contact(
        number=number, **fields, standing_since=since, blocked_attempts=attempts, updated_at=now
    )
