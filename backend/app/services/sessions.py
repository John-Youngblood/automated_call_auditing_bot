"""Dashboard auth: one shared password, exchanged for a session token.

Deliberately small. There is one host and a handful of operators, so there are
no accounts, no roles and no password store -- just ``DASHBOARD_PASSWORD``
traded for an opaque token that the browser sends back.

Tokens live in memory, like everything else here, so a restart logs everyone
out. That is the right trade for a tool whose whole state is already in
memory, and it doubles as revocation: redeploy and every token is dead.

What this does *not* cover, on purpose:

* ``/webhook/*`` -- Twilio carries no credentials of ours. Those are
  authenticated by ``VALIDATE_WEBHOOK_SIGNATURE`` instead, which is a stronger
  check than a shared secret because it is per-request and signed.
* ``/healthz`` -- Cloud Run's probes need it open.
"""

from __future__ import annotations

import logging
import secrets

logger = logging.getLogger(__name__)

#: Shortest password accepted outside local. A shared secret on a public URL
#: is exactly as good as its length, and there is no rate limiting here.
MIN_PASSWORD_LENGTH = 16


class Sessions:
    """Issues and checks dashboard tokens."""

    def __init__(self, password: str) -> None:
        self._password = password
        self._tokens: set[str] = set()

    @property
    def required(self) -> bool:
        """False when no password is set, which leaves the dashboard open.

        Only reachable locally: :func:`app.main.lifespan` refuses to start
        without one anywhere else.
        """
        return bool(self._password)

    def log_in(self, password: str) -> str | None:
        """Exchange the password for a token, or ``None`` if it is wrong.

        ``compare_digest`` rather than ``==``: the comparison is against
        attacker-supplied input, and a short-circuiting compare leaks the
        prefix one character at a time.
        """
        if not self.required or not secrets.compare_digest(password, self._password):
            return None
        token = secrets.token_urlsafe(32)
        self._tokens.add(token)
        logger.info("dashboard session opened (%s active)", len(self._tokens))
        return token

    def is_valid(self, token: str | None) -> bool:
        if not self.required:
            return True
        return bool(token) and token in self._tokens

    def log_out(self, token: str | None) -> None:
        self._tokens.discard(token or "")
