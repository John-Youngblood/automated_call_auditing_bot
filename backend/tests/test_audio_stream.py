"""End-to-end: provider media frames in, transcripts out to the dashboard.

Runs against the mock STT session, so no credentials and no network.
"""

from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

CALL_ID = "CA-audio-1"

#: 20ms of 8kHz mono mu-law, which is exactly what Twilio sends per frame.
CHUNK = bytes(160)


def start_frame(call_id: str = CALL_ID) -> dict:
    return {
        "event": "start",
        "sequenceNumber": "1",
        "streamSid": "MZ-stream-1",
        "start": {
            "streamSid": "MZ-stream-1",
            "callSid": call_id,
            "tracks": ["inbound"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            "customParameters": {"callId": call_id, "from": "+15551230000"},
        },
    }


def media_frame(payload: bytes = CHUNK, chunk_no: int = 1) -> dict:
    return {
        "event": "media",
        "streamSid": "MZ-stream-1",
        "media": {
            "track": "inbound",
            "chunk": str(chunk_no),
            "timestamp": str(chunk_no * 20),
            "payload": base64.b64encode(payload).decode("ascii"),
        },
    }


def test_start_frame_marks_the_call_as_screening(client: TestClient) -> None:
    client.post("/webhook/incoming-call", data={"CallSid": CALL_ID, "From": "+1555"})

    with client.websocket_connect("/ws/frontend") as dashboard:
        dashboard.receive_json()  # snapshot

        with client.websocket_connect("/ws/audio-stream") as audio:
            audio.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
            audio.send_json(start_frame())

            event = dashboard.receive_json()
            assert event["type"] == "call.updated"
            assert event["data"]["call"]["status"] == "screening"
            assert event["data"]["call"]["streamId"] == "MZ-stream-1"


def test_audio_chunks_become_transcript_events(client: TestClient) -> None:
    client.post("/webhook/incoming-call", data={"CallSid": CALL_ID, "From": "+1555"})

    with client.websocket_connect("/ws/frontend") as dashboard:
        dashboard.receive_json()  # snapshot

        with client.websocket_connect("/ws/audio-stream") as audio:
            audio.send_json(start_frame())
            dashboard.receive_json()  # call.updated -> screening

            # Mock STT emits after one second of audio: 8000 bytes = 50 frames.
            for n in range(50):
                audio.send_json(media_frame(chunk_no=n + 1))

            interim = _next_of_type(dashboard, "transcript.delta")
            final = _next_of_type(dashboard, "transcript.delta")

    assert interim["callId"] == CALL_ID
    assert interim["data"]["line"]["isFinal"] is False
    assert final["data"]["line"]["isFinal"] is True
    assert final["data"]["line"]["text"]
    # Interim and final share a segment id, so the UI replaces rather than
    # appends -- this is the property the dashboard's rendering relies on.
    assert interim["data"]["line"]["segmentId"] == final["data"]["line"]["segmentId"]


def test_stream_close_ends_the_call(client: TestClient) -> None:
    client.post("/webhook/incoming-call", data={"CallSid": CALL_ID, "From": "+1555"})

    with client.websocket_connect("/ws/frontend") as dashboard:
        dashboard.receive_json()

        with client.websocket_connect("/ws/audio-stream") as audio:
            audio.send_json(start_frame())
            dashboard.receive_json()
            audio.send_json({"event": "stop", "streamSid": "MZ-stream-1"})

        ended = _next_of_type(dashboard, "call.ended")

    assert ended["data"]["call"]["status"] == "ended"
    assert ended["data"]["call"]["endedAt"]
    assert client.get("/api/calls").json() == []


def test_accepted_call_survives_the_stream_closing(client: TestClient) -> None:
    """Accepting bridges the call elsewhere, which tears down the media stream.
    Teardown must not overwrite the ACCEPTED decision with ENDED."""
    client.post("/webhook/incoming-call", data={"CallSid": CALL_ID, "From": "+1555"})

    with client.websocket_connect("/ws/audio-stream") as audio:
        audio.send_json(start_frame())
        client.post(f"/api/calls/{CALL_ID}/accept")

    assert client.get(f"/api/calls/{CALL_ID}").json()["status"] == "accepted"


def test_unparseable_frames_do_not_kill_the_stream(client: TestClient) -> None:
    client.post("/webhook/incoming-call", data={"CallSid": CALL_ID, "From": "+1555"})

    with client.websocket_connect("/ws/audio-stream") as audio:
        audio.send_text("not json at all")
        audio.send_json({"event": "media", "media": {"payload": "!!!not-base64!!!"}})
        audio.send_json({"event": "surprise"})
        # Still alive and still able to start a call.
        audio.send_json(start_frame())

    assert client.get(f"/api/calls/{CALL_ID}").json()["streamId"] == "MZ-stream-1"


def test_stream_arriving_before_the_webhook_still_works(client: TestClient) -> None:
    """Providers retry; the socket occasionally beats the HTTP callback."""
    with client.websocket_connect("/ws/audio-stream") as audio:
        audio.send_json(start_frame("CA-orphan"))

    call = client.get("/api/calls/CA-orphan")
    assert call.status_code == 200
    assert call.json()["streamId"] == "MZ-stream-1"


def test_dtmf_frame_is_accepted(client: TestClient) -> None:
    with client.websocket_connect("/ws/audio-stream") as audio:
        audio.send_json(start_frame())
        audio.send_json(
            {
                "event": "dtmf",
                "streamSid": "MZ-stream-1",
                "dtmf": {"track": "inbound", "digit": "1"},
            }
        )
        audio.send_json(media_frame())

    assert client.get(f"/api/calls/{CALL_ID}").status_code == 200


def _next_of_type(ws, event_type: str, limit: int = 60) -> dict:
    """Read until an event of ``event_type`` arrives, skipping unrelated ones."""
    for _ in range(limit):
        event = ws.receive_json()
        if event["type"] == event_type:
            return event
    raise AssertionError(f"no {event_type} event within {limit} messages")


def test_outbound_audio_frame_shape() -> None:
    from app.telephony.media_stream import clear_audio_frame, outbound_audio_frame

    frame = json.loads(outbound_audio_frame("MZ-1", b"\xff\xfe"))
    assert frame == {
        "event": "media",
        "streamSid": "MZ-1",
        "media": {"payload": base64.b64encode(b"\xff\xfe").decode()},
    }
    assert json.loads(clear_audio_frame("MZ-1")) == {"event": "clear", "streamSid": "MZ-1"}
