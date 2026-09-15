"""Webhook authenticity checks.

``/webhook/incoming-call`` is a public, unauthenticated URL: anyone who finds
it can fabricate a call. Signature validation is the only thing standing
between the queue and a spoofed-call flood, so turn
``VALIDATE_WEBHOOK_SIGNATURE=true`` before this reaches a real number.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)


def compute_twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Recompute Twilio's ``X-Twilio-Signature``.

    The scheme: take the full request URL, append each POST parameter as
    ``key + value`` in lexicographic key order, HMAC-SHA1 with the account
    auth token, base64-encode.
    """
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(
        auth_token.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_twilio_signature(
    auth_token: str,
    url: str,
    params: dict[str, str],
    signature: str | None,
) -> bool:
    """Constant-time comparison against the provided signature header.

    ``url`` must be the URL Twilio actually requested, including query string
    and the *public* scheme/host. Behind a proxy or tunnel that means
    reconstructing it from forwarded headers -- a mismatch here is the usual
    cause of "valid requests rejected".
    """
    if not auth_token:
        logger.error("signature validation enabled but TWILIO_AUTH_TOKEN is empty")
        return False
    if not signature:
        return False
    expected = compute_twilio_signature(auth_token, url, params)
    return hmac.compare_digest(expected, signature)
