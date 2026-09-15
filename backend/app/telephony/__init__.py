"""Telecom-provider adapters.

Everything provider-specific lives here -- webhook payload shapes, response
dialects (TwiML XML vs NCCO JSON), media-stream framing, signature schemes.
The rest of the app deals only in :mod:`app.schemas` types, so swapping
provider means adding a module here, not editing routes or services.
"""

from app.telephony.base import CallPlan, InstructionRenderer, RenderedResponse
from app.telephony.ncco import NccoRenderer
from app.telephony.twiml import TwimlRenderer

_RENDERERS: dict[str, InstructionRenderer] = {
    "twilio": TwimlRenderer(),
    "vonage": NccoRenderer(),
}


def get_renderer(provider: str) -> InstructionRenderer:
    """Return the response renderer for a configured provider name."""
    try:
        return _RENDERERS[provider]
    except KeyError:
        raise ValueError(
            f"unsupported TELEPHONY_PROVIDER {provider!r}; known: {sorted(_RENDERERS)}"
        ) from None


__all__ = [
    "CallPlan",
    "InstructionRenderer",
    "NccoRenderer",
    "RenderedResponse",
    "TwimlRenderer",
    "get_renderer",
]
