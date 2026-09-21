"""Call domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """snake_case in Python, camelCase on the wire. Serialise with by_alias."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        ser_json_timedelta="float",
    )


class CallStatus(StrEnum):
    #: The greeting has played and the caller is saying why they are calling.
    #: Twilio is listening; there is nothing to act on yet.
    SCREENING = "screening"
    #: Transcript is in; waiting for an operator. Hyphenated because the value
    #: doubles as a CSS class suffix.
    ON_HOLD = "on-hold"
    ACCEPTED = "accepted"
    #: A human declined the call.
    REJECTED = "rejected"
    #: The caller hung up, or the provider reported the call over.
    ENDED = "ended"


#: Statuses a call cannot leave. Defined once so the queue filter and the
#: history view cannot drift apart. Mirrored in frontend/src/types/events.ts.
TERMINAL_STATUSES = frozenset(
    {
        CallStatus.ACCEPTED,
        CallStatus.REJECTED,
        CallStatus.ENDED,
    }
)


class Caller(CamelModel):
    number: str | None = None
    #: The carrier's caller-ID name. Only present when Caller ID Lookup is
    #: enabled on the Twilio number (a paid per-lookup feature, off by
    #: default), and even then often generic ("WIRELESS CALLER").
    name: str | None = None
    #: Short label for where the *number* is registered, e.g. "Portland, OR".
    #: Formatted server-side by app.services.phone.format_location so the rule
    #: for picking state vs country lives in one language, not two.
    location: str | None = None


class Call(CamelModel):
    """Public view of a call. This is what the dashboard renders."""

    call_id: str
    status: CallStatus = CallStatus.SCREENING
    caller: Caller = Field(default_factory=Caller)
    to_number: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None

    #: What the caller said when asked why they are calling. Populated in one
    #: shot by the speech-result webhook once they stop speaking, so there is
    #: no partial state to reconcile -- it is either absent or complete.
    transcript: str | None = None
    #: Provider's confidence in that transcription, 0-1. Worth surfacing: a
    #: low score means the operator should not trust the text they are about
    #: to make a decision from.
    transcript_confidence: float | None = None

    #: Sticky, unlike ACCEPTED -- which becomes ENDED once the bridge
    #: finishes. Without this, history cannot tell a caller who made it on air
    #: from one who hung up while being screened; both read "ended".
    was_accepted: bool = False

    #: Twilio's DialCallDuration. None alongside ``was_accepted`` means the
    #: bridge never connected, usually because the host was already on a call.
    on_air_seconds: int | None = None

    #: Rebuilt from Twilio after a restart, so genuinely on hold but with no
    #: transcript -- which otherwise looks identical to a caller who said
    #: nothing. See app/services/reconcile.py.
    recovered: bool = False

    @property
    def is_open(self) -> bool:
        """Still in the live screening queue. ACCEPTED counts as closed."""
        return self.status not in TERMINAL_STATUSES
