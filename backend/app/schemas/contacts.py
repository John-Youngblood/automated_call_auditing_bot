"""Wire contracts for named callers and favourites."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from app.schemas.calls import CamelModel


class ContactUpsertRequest(CamelModel):
    """Body of ``POST /api/contacts``.

    Both fields are optional and ``None`` means "leave as-is", so the star
    button can toggle a favourite without touching a name someone else typed,
    and the name dialog can save a name without disturbing the star. Send an
    empty string to actually clear a name.
    """

    number: str = Field(min_length=3, max_length=64)
    display_name: str | None = Field(default=None, max_length=128)
    is_favorite: bool | None = None

    @model_validator(mode="after")
    def _require_something_to_do(self) -> ContactUpsertRequest:
        if self.display_name is None and self.is_favorite is None:
            raise ValueError("provide displayName, isFavorite, or both")
        return self


class ContactOut(CamelModel):
    number: str
    display_name: str | None = None
    is_favorite: bool = False
    created_at: datetime
    updated_at: datetime
