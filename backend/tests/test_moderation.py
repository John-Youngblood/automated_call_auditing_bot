"""Blocking: the webhook gate, the block endpoint, and the history view."""

from __future__ import annotations

from xml.etree.ElementTree import fromstring

from fastapi.testclient import TestClient

BLOCKED_CALLER = "+15550192834"

INCOMING = {
    "CallSid": "CA-mod-1",
    "From": BLOCKED_CALLER,
    "To": "+15039990000",
    "CallerName": "Repeat Caller",
    "FromCity": "Portland",
    "FromCountry": "US",
}


def ring(client: TestClient, **overrides: str) -> str:
    """Place an inbound call; returns the raw provider response body."""
    return client.post("/webhook/incoming-call", data={**INCOMING, **overrides}).text


class TestBlocklistGate:
    def test_blocked_caller_is_refused_without_being_answered(self, client: TestClient) -> None:
        client.post("/api/block-number", json={"number": BLOCKED_CALLER})

        body = ring(client)

        root = fromstring(body)
        assert [child.tag for child in root] == ["Reject"]
        # The important negatives: never answered, so no greeting plays, no
        # transcription is requested, and no per-minute charge is incurred.
        assert root.find("Gather") is None
        assert root.find("Enqueue") is None

    def test_blocked_caller_never_reaches_the_live_queue(self, client: TestClient) -> None:
        client.post("/api/block-number", json={"number": BLOCKED_CALLER})

        ring(client)

        assert client.get("/api/calls").json() == []

    def test_blocked_attempt_is_still_recorded_in_history(self, client: TestClient) -> None:
        """A block that hides evidence of repeat harassment is worse than none."""
        client.post("/api/block-number", json={"number": BLOCKED_CALLER})

        ring(client, CallSid="CA-attempt-1")
        ring(client, CallSid="CA-attempt-2")

        history = client.get("/api/call-history").json()
        assert [row["callId"] for row in history] == ["CA-attempt-2", "CA-attempt-1"]
        assert all(row["status"] == "blocked" for row in history)
        assert all(row["isBlocked"] is True for row in history)

    def test_unblocked_caller_still_gets_the_normal_treatment(self, client: TestClient) -> None:
        root = fromstring(ring(client))

        assert root.find("Gather") is not None
        assert root.find("Reject") is None
        assert len(client.get("/api/calls").json()) == 1

    def test_block_matches_regardless_of_formatting(self, client: TestClient) -> None:
        """Moderator types a pretty number; the carrier sends E.164."""
        client.post("/api/block-number", json={"number": "(555) 019-2834"})

        root = fromstring(ring(client, **{"From": "+15550192834"}))
        assert root.find("Reject") is not None

    def test_withheld_caller_id_is_not_blocked(self, client: TestClient) -> None:
        """Blocking 'unknown' would bar every anonymous caller at once."""
        client.post("/api/block-number", json={"number": BLOCKED_CALLER})

        root = fromstring(ring(client, CallSid="CA-anon", From=""))
        assert root.find("Gather") is not None

    def test_reject_reason_is_configurable(self, make_client) -> None:
        with make_client(BLOCKED_CALL_REJECT_REASON="busy") as client:
            client.post("/api/block-number", json={"number": BLOCKED_CALLER})
            root = fromstring(ring(client))

        assert root.find("Reject").attrib["reason"] == "busy"


class TestBlockEndpoint:
    def test_blocking_persists_and_normalises(self, client: TestClient) -> None:
        response = client.post(
            "/api/block-number",
            json={"number": "(555) 019-2834", "reason": "abusive", "blockedBy": "jsy"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["newlyBlocked"] is True
        assert body["blocked"]["number"] == "+15550192834"
        # Kept so "I blocked them and they still got through" is debuggable.
        assert body["blocked"]["originalInput"] == "(555) 019-2834"
        assert body["blocked"]["reason"] == "abusive"

        listed = client.get("/api/blocked-numbers").json()
        assert [row["number"] for row in listed] == ["+15550192834"]

    def test_blocking_twice_is_not_an_error(self, client: TestClient) -> None:
        client.post("/api/block-number", json={"number": BLOCKED_CALLER})
        second = client.post("/api/block-number", json={"number": BLOCKED_CALLER})

        assert second.status_code == 200
        assert second.json()["newlyBlocked"] is False
        # One row, not two -- a double-click must not duplicate the entry.
        assert len(client.get("/api/blocked-numbers").json()) == 1

    def test_unusable_number_is_a_400(self, client: TestClient) -> None:
        response = client.post("/api/block-number", json={"number": "nonsense"})

        assert response.status_code == 400
        assert client.get("/api/blocked-numbers").json() == []

    def test_blocking_hangs_up_the_caller_s_live_call(self, client: TestClient) -> None:
        ring(client)
        assert len(client.get("/api/calls").json()) == 1

        response = client.post("/api/block-number", json={"number": BLOCKED_CALLER})

        assert response.json()["terminatedCallIds"] == ["CA-mod-1"]
        assert response.json()["failedCallIds"] == []
        # Dropped from the live queue and recorded as blocked.
        assert client.get("/api/calls").json() == []
        assert client.get("/api/calls/CA-mod-1").json()["status"] == "blocked"

    def test_live_hangup_is_broadcast_to_dashboards(self, client: TestClient) -> None:
        ring(client)

        with client.websocket_connect("/ws/frontend") as ws:
            ws.receive_json()  # snapshot
            client.post("/api/block-number", json={"number": BLOCKED_CALLER})
            event = ws.receive_json()

        assert event["type"] == "call.ended"
        assert event["data"]["call"]["status"] == "blocked"

    def test_blocking_only_touches_that_caller_s_calls(self, client: TestClient) -> None:
        ring(client)
        ring(client, CallSid="CA-other", From="+15550001111")

        response = client.post("/api/block-number", json={"number": BLOCKED_CALLER})

        assert response.json()["terminatedCallIds"] == ["CA-mod-1"]
        assert [c["callId"] for c in client.get("/api/calls").json()] == ["CA-other"]

    def test_re_blocking_still_drops_a_new_live_call(self, client: TestClient) -> None:
        """The 'kick them off now' case: already blocked, but got through on a
        call placed before the block, or from a second line."""
        client.post("/api/block-number", json={"number": BLOCKED_CALLER})
        # Simulate a call already in flight when the block landed.
        client.post("/webhook/incoming-call", data={**INCOMING, "From": "+15550009999"})
        client.post("/api/block-number", json={"number": "+15550009999"})

        assert client.get("/api/calls").json() == []


class TestCallHistory:
    def test_finished_calls_appear_with_transcript_summary(self, client: TestClient) -> None:
        ring(client)
        client.post("/api/calls/CA-mod-1/reject")

        rows = client.get("/api/call-history").json()

        assert len(rows) == 1
        row = rows[0]
        assert row["callId"] == "CA-mod-1"
        assert row["status"] == "rejected"
        assert row["fromNumber"] == "+15550192834"
        assert row["fromName"] == "Repeat Caller"
        assert row["fromLocation"] == "Portland, US"
        assert row["endedAt"] is not None
        assert row["durationSeconds"] is not None

    def test_in_flight_calls_are_not_in_history(self, client: TestClient) -> None:
        ring(client)

        assert client.get("/api/call-history").json() == []

    def test_history_row_is_upserted_not_duplicated(self, client: TestClient) -> None:
        """A call is rejected, then the provider's status callback confirms it
        ended. That is one call, not two."""
        ring(client)
        client.post("/api/calls/CA-mod-1/reject")
        client.post(
            "/webhook/call-status",
            data={"CallSid": "CA-mod-1", "CallStatus": "completed"},
        )

        rows = client.get("/api/call-history").json()
        assert len(rows) == 1
        # The human's decision survives the later provider callback.
        assert rows[0]["status"] == "rejected"

    def test_history_is_newest_first_and_respects_limit(self, client: TestClient) -> None:
        for n in range(3):
            ring(client, CallSid=f"CA-{n}", From=f"+1555000{n:04d}")
            client.post(f"/api/calls/CA-{n}/reject")

        rows = client.get("/api/call-history", params={"limit": 2}).json()

        assert [row["callId"] for row in rows] == ["CA-2", "CA-1"]

    def test_is_blocked_flag_reflects_the_current_blocklist(self, client: TestClient) -> None:
        """Drives the history view's Block button state."""
        ring(client)
        client.post("/api/calls/CA-mod-1/reject")

        assert client.get("/api/call-history").json()[0]["isBlocked"] is False

        client.post("/api/block-number", json={"number": BLOCKED_CALLER})

        assert client.get("/api/call-history").json()[0]["isBlocked"] is True

    def test_retroactive_block_of_a_hung_up_caller(self, client: TestClient) -> None:
        """The whole point of Block in the history view: the caller is long
        gone, so there is nothing to hang up, but future calls are refused."""
        ring(client)
        client.post("/webhook/call-status", data={"CallSid": "CA-mod-1", "CallStatus": "completed"})

        response = client.post("/api/block-number", json={"number": BLOCKED_CALLER})

        assert response.json()["terminatedCallIds"] == []
        assert fromstring(ring(client, CallSid="CA-later")).find("Reject") is not None


class TestTimestamps:
    def test_history_timestamps_round_trip_as_utc(self, client: TestClient) -> None:
        """SQLite has no tz-aware datetime type, so an unguarded column returns
        a naive value, Pydantic drops the offset, and the browser reads it as
        local time -- shifting every entry in the moderation log."""
        ring(client)
        client.post("/api/calls/CA-mod-1/reject")

        row = client.get("/api/call-history").json()[0]

        for field in ("startedAt", "endedAt"):
            assert row[field].endswith("Z") or "+00:00" in row[field], (
                f"{field}={row[field]!r} carries no timezone, so clients will read it as local"
            )

    def test_history_time_matches_the_live_call(self, client: TestClient) -> None:
        """The same call must report the same instant in both views."""
        from datetime import datetime

        ring(client)
        live = client.get("/api/calls/CA-mod-1").json()["startedAt"]
        client.post("/api/calls/CA-mod-1/reject")
        stored = client.get("/api/call-history").json()[0]["startedAt"]

        assert datetime.fromisoformat(live) == datetime.fromisoformat(stored)

    def test_blocked_at_timestamp_is_utc(self, client: TestClient) -> None:
        created = client.post("/api/block-number", json={"number": BLOCKED_CALLER}).json()
        value = created["blocked"]["createdAt"]

        assert value.endswith("Z") or "+00:00" in value
