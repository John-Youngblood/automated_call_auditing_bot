"""Talking to Twilio's REST API.

Most of this package is Twilio talking to us. This is the other direction:
asking what Twilio currently knows, and telling it to change a live call.

    GET  /Queues.json                        find the hold queue by name
    GET  /Queues/{sid}/Members.json          who is waiting in it
    GET  /Calls/{sid}.json                   who that caller actually is
    POST /Calls/{sid}.json                   send a live call new instructions

The reads serve startup reconciliation; the write serves closing the line at
the end of a show. See app/services/reconcile.py and app/services/drain.py.

Deliberately hand-rolled on ``httpx`` rather than ``twilio-python``: we need
four endpoints, the official SDK is synchronous (requests-based, no asyncio
client), and dropping a blocking call into the loop that serves the dashboard
sockets would be a worse trade than fifty lines of HTTP.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

logger = logging.getLogger(__name__)

API_ROOT = "https://api.twilio.com/2010-04-01"

#: Pages of queue members to walk before giving up. Twilio caps a queue at
#: 5000 and pages at 50, so this covers a full queue with room to spare while
#: still bounding a pagination bug into a finite loop.
_MAX_PAGES = 120


@dataclass(slots=True, frozen=True)
class QueueMember:
    call_sid: str
    #: When Twilio enqueued them. The caller's real wait started earlier -- it
    #: began when they dialled -- so this understates their time on hold.
    enqueued_at: datetime | None
    position: int
    wait_time_seconds: int


@dataclass(slots=True, frozen=True)
class CallDetails:
    call_sid: str
    from_number: str | None
    to_number: str | None
    #: Only populated when Caller ID Lookup is enabled on the number.
    caller_name: str | None
    started_at: datetime | None
    status: str


def _parse_rfc2822(value: str | None) -> datetime | None:
    """Twilio timestamps are RFC 2822 (``Tue, 21 Sep 2026 20:14:02 +0000``)."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        logger.warning("could not parse Twilio timestamp %r", value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class TwilioRestClient:
    """Client for the handful of endpoints this service actually uses.

    Not a general Twilio SDK and not trying to be. The accept/reject commands
    are still stubs in :mod:`app.telephony.provider_client`; when those become
    real they belong here, and that module goes away.
    """

    def __init__(
        self,
        *,
        account_sid: str,
        username: str,
        password: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._account_sid = account_sid
        self._auth = (username, password)
        self._timeout = timeout_seconds

    async def __aenter__(self) -> TwilioRestClient:
        self._client = httpx.AsyncClient(auth=self._auth, timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _request(
        self, method: str, path: str, data: dict[str, str] | None = None
    ) -> dict | None:
        """One request. Returns ``None`` for anything that is not a usable 2xx.

        Swallows rather than raises: every caller is best-effort, where the
        right response to Twilio being unavailable is to log it and report the
        failure upwards rather than turn it into a 500.
        """
        url = path if path.startswith("http") else f"{API_ROOT}/Accounts/{self._account_sid}{path}"
        try:
            response = await self._client.request(method, url, data=data)
        except httpx.HTTPError as exc:
            logger.warning("twilio %s %s failed: %s", method, path, exc)
            return None

        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        if response.is_error:
            # 401 here is the common one and worth naming: it means the REST
            # credentials are wrong, which is a different problem from Twilio
            # being down and wants a different fix.
            logger.warning(
                "twilio %s %s returned %s: %s",
                method,
                path,
                response.status_code,
                response.text[:200],
            )
            return None
        return response.json()

    async def _get(self, path: str) -> dict | None:
        return await self._request("GET", path)

    async def find_queue_sid(self, friendly_name: str) -> str | None:
        """The SID of the queue with this name, or ``None`` if it has none.

        ``<Enqueue>`` creates queues on demand by name, so the SID is not known
        anywhere in this codebase until we ask. A missing queue is the normal
        case on a cold account and not an error.
        """
        payload = await self._get("/Queues.json?PageSize=1000")
        if payload is None:
            return None
        for queue in payload.get("queues") or []:
            if queue.get("friendly_name") == friendly_name:
                logger.info(
                    "hold queue %r sid=%s size=%s/%s",
                    friendly_name,
                    queue.get("sid"),
                    queue.get("current_size"),
                    queue.get("max_size"),
                )
                return queue.get("sid")
        logger.info("no Twilio queue named %r yet", friendly_name)
        return None

    async def list_queue_members(self, queue_sid: str) -> list[QueueMember]:
        """Everyone currently waiting in the queue, following pagination."""
        members: list[QueueMember] = []
        path: str | None = f"/Queues/{queue_sid}/Members.json?PageSize=50"

        for _ in range(_MAX_PAGES):
            if path is None:
                break
            payload = await self._get(path)
            if payload is None:
                break
            for row in payload.get("queue_members") or []:
                call_sid = row.get("call_sid")
                if not call_sid:
                    continue
                members.append(
                    QueueMember(
                        call_sid=call_sid,
                        enqueued_at=_parse_rfc2822(row.get("date_enqueued")),
                        position=row.get("position") or 0,
                        wait_time_seconds=row.get("wait_time") or 0,
                    )
                )
            next_uri = payload.get("next_page_uri")
            path = f"https://api.twilio.com{next_uri}" if next_uri else None

        return members

    async def fetch_call(self, call_sid: str) -> CallDetails | None:
        """Who the caller is.

        Note what is *not* here: ``FromCity``/``FromState``/``FromCountry``.
        Those are webhook parameters, not fields on the Call resource, so a
        recovered call has no location label. Nothing to do about it -- Twilio
        does not keep them.
        """
        payload = await self._get(f"/Calls/{call_sid}.json")
        if payload is None:
            return None
        return CallDetails(
            call_sid=payload.get("sid") or call_sid,
            from_number=payload.get("from") or None,
            to_number=payload.get("to") or None,
            caller_name=payload.get("caller_name") or None,
            started_at=_parse_rfc2822(payload.get("start_time")),
            status=payload.get("status") or "unknown",
        )

    async def end_call(self, call_sid: str, twiml: str) -> bool:
        """Pull a live call out of whatever it is doing and run ``twiml``.

        Used to close the line: the document says goodbye and hangs up, which
        is meaningfully better than ``Status=completed``. A silent drop is
        indistinguishable from a dropped connection to the person on the other
        end, and someone who has been on hold for ten minutes deserves to be
        told the show is over rather than left wondering.

        A 404 counts as success. It means the call is already gone, and the
        goal here is "this caller is no longer holding", which is satisfied.
        """
        payload = await self._request("POST", f"/Calls/{call_sid}.json", data={"Twiml": twiml})
        if payload is not None:
            return True

        # _request already logged the reason. Distinguish "already ended" from
        # a real failure so the operator is not told a hang-up failed when the
        # caller had simply hung up first.
        probe = await self.fetch_call(call_sid)
        return probe is None or probe.status not in {"in-progress", "ringing", "queued"}
