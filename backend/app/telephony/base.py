"""Provider-neutral description of what should happen to an inbound call."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class CallPlan:
    """The three instructions the webhook hands back to the provider:

    1. answer the call (implicit -- returning a document at all answers it),
    2. play ``greeting_url``,
    3. connect a two-way media stream to ``stream_url``.
    """

    call_id: str
    greeting_url: str
    stream_url: str
    #: Passed through the provider so the media websocket can identify the call
    #: without trusting anything the socket itself claims.
    stream_parameters: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class RenderedResponse:
    body: str
    media_type: str


class InstructionRenderer(Protocol):
    """Serialises call-control instructions into one provider's dialect."""

    name: str

    def render(self, plan: CallPlan) -> RenderedResponse: ...

    def render_reject(self, reason: str = "rejected") -> RenderedResponse:
        """Refuse the call outright.

        Returned for blocked callers. The point is that no media stream is
        opened and no transcription session starts, so a blocked number costs
        nothing beyond the inbound signalling the carrier does anyway.
        """
        ...
