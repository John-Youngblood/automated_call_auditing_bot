"""The dashboard websocket: snapshot on connect, live events, commands back."""

from __future__ import annotations

from fastapi.testclient import TestClient

INCOMING = {"CallSid": "CA-dash-1", "From": "+15551112222", "To": "+15559990000"}


def test_snapshot_is_sent_immediately_on_connect(client: TestClient) -> None:
    client.post("/webhook/incoming-call", data=INCOMING)

    with client.websocket_connect("/ws/frontend") as ws:
        event = ws.receive_json()

    # A dashboard that connects mid-call must not start blank.
    assert event["type"] == "state.snapshot"
    assert [c["callId"] for c in event["data"]["calls"]] == ["CA-dash-1"]


def test_incoming_call_is_pushed_to_connected_dashboards(client: TestClient) -> None:
    with client.websocket_connect("/ws/frontend") as ws:
        snapshot = ws.receive_json()
        assert snapshot["data"]["calls"] == []

        client.post("/webhook/incoming-call", data=INCOMING)

        event = ws.receive_json()

    assert event["type"] == "call.incoming"
    assert event["callId"] == "CA-dash-1"
    assert event["data"]["call"]["caller"]["number"] == "+15551112222"
    assert event["ts"]


def test_every_dashboard_sees_the_same_events(client: TestClient) -> None:
    with (
        client.websocket_connect("/ws/frontend") as first,
        client.websocket_connect("/ws/frontend") as second,
    ):
        first.receive_json()
        second.receive_json()

        client.post("/webhook/incoming-call", data=INCOMING)

        assert first.receive_json()["callId"] == "CA-dash-1"
        assert second.receive_json()["callId"] == "CA-dash-1"


def test_ping_is_answered_with_pong(client: TestClient) -> None:
    with client.websocket_connect("/ws/frontend") as ws:
        ws.receive_json()
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_accept_command_updates_status_and_broadcasts(client: TestClient) -> None:
    client.post("/webhook/incoming-call", data=INCOMING)

    with client.websocket_connect("/ws/frontend") as ws:
        ws.receive_json()
        ws.send_json({"type": "call.accept", "callId": "CA-dash-1"})
        event = ws.receive_json()

    assert event["type"] == "call.updated"
    assert event["data"]["call"]["status"] == "accepted"


def test_reject_command_ends_the_call(client: TestClient) -> None:
    client.post("/webhook/incoming-call", data=INCOMING)

    with client.websocket_connect("/ws/frontend") as ws:
        ws.receive_json()
        ws.send_json({"type": "call.reject", "callId": "CA-dash-1"})
        event = ws.receive_json()

    assert event["type"] == "call.ended"
    assert event["data"]["call"]["status"] == "rejected"
    # Rejected calls leave the queue.
    assert client.get("/api/calls").json() == []


def test_malformed_command_gets_an_error_not_a_disconnect(client: TestClient) -> None:
    with client.websocket_connect("/ws/frontend") as ws:
        ws.receive_json()

        ws.send_json({"type": "definitely-not-a-command"})
        assert ws.receive_json()["type"] == "error"

        # Connection survives, so one bad frame cannot knock a dashboard out.
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_accept_for_unknown_call_reports_an_error(client: TestClient) -> None:
    with client.websocket_connect("/ws/frontend") as ws:
        ws.receive_json()
        ws.send_json({"type": "call.accept", "callId": "nope"})
        event = ws.receive_json()

    assert event["type"] == "error"
    assert event["callId"] == "nope"


def test_rest_accept_matches_the_websocket_command(client: TestClient) -> None:
    client.post("/webhook/incoming-call", data=INCOMING)

    with client.websocket_connect("/ws/frontend") as ws:
        ws.receive_json()
        response = client.post("/api/calls/CA-dash-1/accept")
        event = ws.receive_json()

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    # The decision reaches dashboards regardless of which channel made it.
    assert event["data"]["call"]["status"] == "accepted"


def test_rest_decision_on_unknown_call_is_404(client: TestClient) -> None:
    assert client.post("/api/calls/missing/accept").status_code == 404


def test_decision_stamps_ended_at_so_the_queue_timer_freezes(client: TestClient) -> None:
    """A decided call is out of screening. Without an `endedAt`, the dashboard
    has no end point to measure against and counts up forever."""
    client.post("/webhook/incoming-call", data=INCOMING)
    assert client.get(f"/api/calls/{INCOMING['CallSid']}").json()["endedAt"] is None

    accepted = client.post(f"/api/calls/{INCOMING['CallSid']}/accept").json()
    assert accepted["endedAt"] is not None


def test_ended_at_is_not_overwritten_by_a_later_transition(client: TestClient) -> None:
    client.post("/webhook/incoming-call", data=INCOMING)
    first = client.post(f"/api/calls/{INCOMING['CallSid']}/accept").json()["endedAt"]

    # Provider status callback arrives afterwards; the original decision time
    # is what the operator saw, so it must stand.
    client.post(
        "/webhook/call-status",
        data={"CallSid": INCOMING["CallSid"], "CallStatus": "completed"},
    )
    assert client.get(f"/api/calls/{INCOMING['CallSid']}").json()["endedAt"] == first


def test_accepted_call_leaves_the_live_queue(client: TestClient) -> None:
    """An accepted call is handed to a human and is no longer being screened.
    Leaving it in `open_calls` meant it rode along in every snapshot for the
    life of the process."""
    client.post("/webhook/incoming-call", data=INCOMING)
    assert len(client.get("/api/calls").json()) == 1

    client.post(f"/api/calls/{INCOMING['CallSid']}/accept")

    assert client.get("/api/calls").json() == []
    # Still individually addressable, and still accepted.
    assert client.get(f"/api/calls/{INCOMING['CallSid']}").json()["status"] == "accepted"


def test_finished_calls_do_not_accumulate_in_memory(client: TestClient) -> None:
    """Nothing else evicts from the in-memory registry, so a long-running
    process would hold every call it ever saw."""
    from app.services.call_registry import _RETAINED_TERMINAL_CALLS

    overflow = _RETAINED_TERMINAL_CALLS + 10
    for n in range(overflow):
        call_id = f"CA-bulk-{n}"
        client.post("/webhook/incoming-call", data={**INCOMING, "CallSid": call_id})
        client.post(f"/api/calls/{call_id}/reject")

    # Oldest evicted from memory...
    assert client.get("/api/calls/CA-bulk-0").status_code == 404
    # ...but still durable in history, which is what the moderator reads.
    history = client.get("/api/call-history", params={"limit": 500}).json()
    assert len(history) == overflow
