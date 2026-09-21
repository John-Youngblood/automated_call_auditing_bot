"""Call domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Base model that speaks snake_case in Python and camelCase on the wire.

    Keeps the TypeScript client idiomatic without littering the Python side
    with aliases. Always serialise with ``by_alias=True``.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        ser_json_timedelta="float",
    )


class CallStatus(StrEnum):
    #: The greeting has played and the caller is saying why they are calling.
    #: Twilio is listening; there is nothing to act on yet.
    SCREENING = "screening"
    #: The caller has finished and their transcript is on the dashboard. They
    #: are on hold, waiting for an operator to decide.
    #: Hyphenated because this value is also used as a CSS class suffix.
    ON_HOLD = "on-hold"
    ACCEPTED = "accepted"
    #: A human declined the call.
    REJECTED = "rejected"
    #: The caller hung up, or the provider reported the call over.
    ENDED = "ended"


#: Statuses a call cannot leave. Defined once here so the queue filter and the
#: history view cannot drift apart -- they did once, and a resolved call stayed
#: in the live queue as a result.
#: Mirrored in frontend/src/types/events.ts.
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

    #: True once an operator pressed Accept. Survives the call ending, which
    #: ACCEPTED does not -- that status means "on air right now" and is
    #: replaced by ENDED when the bridge finishes. Without this, history could
    #: not tell a caller who made it on air from one who hung up while being
    #: screened: both end up ENDED.
    was_accepted: bool = False

    #: Seconds the caller actually spent talking to the host, from Twilio's
    #: DialCallDuration. None alongside ``was_accepted`` is meaningful rather
    #: than missing: it says the bridge never connected -- the usual cause
    #: being the host already on air with someone else.
    on_air_seconds: int | None = None

    #: True when this call was rebuilt from Twilio at startup rather than seen
    #: arrive. Such a call is genuinely on hold, but its transcript died with
    #: the previous process -- so the dashboard has to say so rather than
    #: showing a silent caller with no stated reason, which looks identical to
    #: someone who said nothing.
    recovered: bool = False

    @property
    def is_open(self) -> bool:
        """Whether the call still belongs in the live screening queue.

        ACCEPTED counts as closed: the call may well continue with a human,
        but it is no longer being screened, and leaving it here meant accepted
        calls accumulated in every snapshot for the life of the process.
        """
        return self.status not in TERMINAL_STATUSES
