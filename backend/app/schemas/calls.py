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
    #: Webhook received, greeting playing, waiting on a human decision.
    RINGING = "ringing"
    #: Audio is flowing and being transcribed.
    SCREENING = "screening"
    ACCEPTED = "accepted"
    #: A human declined the call.
    REJECTED = "rejected"
    #: Provider hung up / stream closed.
    ENDED = "ended"
    #: Terminated because the caller is on the blocklist. Distinct from
    #: REJECTED on purpose: moderators need to tell "we turned them away this
    #: time" apart from "this number is barred", and repeat attempts by a
    #: blocked caller are exactly the signal worth seeing in history.
    BLOCKED = "blocked"


#: Statuses a call cannot leave. Defined once here so the registry, the queue
#: filter and the history writer cannot drift apart -- they did, and a blocked
#: call stayed in the live queue as a result.
#: Mirrored in frontend/src/types/events.ts.
TERMINAL_STATUSES = frozenset(
    {
        CallStatus.ACCEPTED,
        CallStatus.REJECTED,
        CallStatus.ENDED,
        CallStatus.BLOCKED,
    }
)


class TranscriptLine(CamelModel):
    """One chunk of recognised speech.

    Deepgram emits interim hypotheses before it commits, so a line starts with
    ``is_final=False`` and is replaced in place until the final arrives. The
    dashboard keys on ``segment_id`` to do that replacement.
    """

    segment_id: int
    text: str
    is_final: bool = False
    speaker: str | None = None
    confidence: float | None = None
    #: Seconds from the start of the media stream.
    start_time: float | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Caller(CamelModel):
    number: str | None = None
    name: str | None = None
    city: str | None = None
    country: str | None = None


class Call(CamelModel):
    """Public view of a call. This is what the dashboard renders."""

    call_id: str
    status: CallStatus = CallStatus.RINGING
    caller: Caller = Field(default_factory=Caller)
    to_number: str | None = None
    #: Provider's media-stream identifier, present once streaming begins.
    stream_id: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    transcript: list[TranscriptLine] = Field(default_factory=list)

    @property
    def is_open(self) -> bool:
        """Whether the call still belongs in the live screening queue.

        ACCEPTED counts as closed: the call may well continue with a human,
        but it is no longer being screened, and leaving it here meant accepted
        calls accumulated in every snapshot for the life of the process.
        """
        return self.status not in TERMINAL_STATUSES
