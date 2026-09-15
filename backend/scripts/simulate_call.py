#!/usr/bin/env python3
"""Drive a fake inbound call against a locally running backend.

Replays what the telecom provider does: posts the webhook, opens the media
websocket, streams audio frames on a 20ms clock, then hangs up. Lets you
develop the dashboard without a phone number, a tunnel, or a real caller.

    python scripts/simulate_call.py                 # one 12-second call
    python scripts/simulate_call.py --seconds 30    # longer
    python scripts/simulate_call.py --calls 3       # a small queue

The audio is silence. With STT_MOCK=true (the default) the backend synthesises
transcript lines from it, which is enough to exercise the whole pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import random
import string
import sys
from urllib.parse import urlparse

import httpx
import websockets

#: Twilio media stream cadence: one frame per 20ms.
FRAME_INTERVAL_SECONDS = 0.02
#: 20ms of 8kHz mono mu-law.
FRAME_BYTES = 160
SILENCE = bytes(FRAME_BYTES)


def random_suffix(length: int = 6) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def websocket_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/ws/audio-stream"


async def place_call(base_url: str, call_id: str, from_number: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{base_url}/webhook/incoming-call",
            data={
                "CallSid": call_id,
                "From": from_number,
                "To": "+15039990000",
                "CallerName": "Simulated Caller",
                "FromCity": "Portland",
                "FromCountry": "US",
                "CallStatus": "ringing",
            },
        )
        response.raise_for_status()
        print(f"[{call_id}] webhook accepted, provider instructions:\n{response.text}\n")


async def stream_audio(base_url: str, call_id: str, from_number: str, seconds: float) -> None:
    stream_id = f"MZ{random_suffix(24)}"
    url = websocket_url(base_url)

    async with websockets.connect(url) as socket:
        await socket.send(
            json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        )
        await socket.send(
            json.dumps(
                {
                    "event": "start",
                    "sequenceNumber": "1",
                    "streamSid": stream_id,
                    "start": {
                        "streamSid": stream_id,
                        "callSid": call_id,
                        "tracks": ["inbound"],
                        "mediaFormat": {
                            "encoding": "audio/x-mulaw",
                            "sampleRate": 8000,
                            "channels": 1,
                        },
                        "customParameters": {"callId": call_id, "from": from_number},
                    },
                }
            )
        )
        print(f"[{call_id}] streaming {seconds:g}s of audio on {stream_id}")

        payload = base64.b64encode(SILENCE).decode("ascii")
        frames = int(seconds / FRAME_INTERVAL_SECONDS)
        loop = asyncio.get_running_loop()
        next_send = loop.time()

        for index in range(frames):
            await socket.send(
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": stream_id,
                        "media": {
                            "track": "inbound",
                            "chunk": str(index + 1),
                            "timestamp": str(index * 20),
                            "payload": payload,
                        },
                    }
                )
            )
            # Absolute schedule rather than sleep(0.02): keeps the stream on the
            # real 20ms clock instead of drifting by the send cost each frame.
            next_send += FRAME_INTERVAL_SECONDS
            await asyncio.sleep(max(0, next_send - loop.time()))

        await socket.send(json.dumps({"event": "stop", "streamSid": stream_id}))
        print(f"[{call_id}] hung up")


async def run_one(base_url: str, seconds: float, index: int) -> None:
    call_id = f"CA{random_suffix(20)}"
    from_number = f"+1503555{random.randint(1000, 9999)}"
    # Stagger so the queue fills up the way a real one does.
    await asyncio.sleep(index * 1.5)
    await place_call(base_url, call_id, from_number)
    await stream_audio(base_url, call_id, from_number, seconds)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--seconds", type=float, default=12.0, help="call duration")
    parser.add_argument("--calls", type=int, default=1, help="concurrent calls to place")
    args = parser.parse_args()

    try:
        await asyncio.gather(*(run_one(args.base_url, args.seconds, i) for i in range(args.calls)))
    except (httpx.HTTPError, OSError) as exc:
        print(f"could not reach {args.base_url}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
