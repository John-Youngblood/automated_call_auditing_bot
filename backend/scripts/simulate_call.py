#!/usr/bin/env python3
"""Drive a fake inbound call against a locally running backend.

Replays what Twilio does: posts the inbound-call webhook, then posts the
speech result Twilio would send once the caller stopped talking. Lets you
work on the dashboard with no phone number, no tunnel, and no real caller.

    python scripts/simulate_call.py                 # one call
    python scripts/simulate_call.py --calls 3       # a small queue
    python scripts/simulate_call.py --say "I need to reschedule an appointment"
"""

from __future__ import annotations

import argparse
import asyncio
import random
import string
import sys

import httpx

REASONS = [
    "Hi, this is Dana from Northgate Medical billing. I'm following up on an "
    "outstanding invoice for one of your patients and I need to confirm some details.",
    "I'm calling to reschedule my appointment next Tuesday, something came up at work "
    "and I can't make the morning slot.",
    "Yeah, hi, I've been on hold with you people three times today and nobody can tell me "
    "why my prescription hasn't been sent to the pharmacy.",
    "Hello, I'd like to ask about the results from the blood work I had done last week.",
]


def call_sid() -> str:
    return "CA" + "".join(random.choices(string.ascii_lowercase + string.digits, k=20))


async def place_call(client: httpx.AsyncClient, base: str, reason: str, delay: float) -> None:
    await asyncio.sleep(delay)
    sid = call_sid()
    number = f"+1503555{random.randint(1000, 9999)}"

    answer = await client.post(
        f"{base}/webhook/incoming-call",
        data={
            "CallSid": sid,
            "From": number,
            "To": "+15039990000",
            "CallerName": "Simulated Caller",
            "FromCity": "Portland",
            "FromCountry": "US",
            "CallStatus": "ringing",
        },
    )
    answer.raise_for_status()

    if "<Reject" in answer.text:
        print(f"[{sid}] blocked at the carrier -- caller is on the blocklist")
        return
    print(f"[{sid}] ringing from {number}, greeting playing")

    # Twilio would spend this time playing the greeting and listening.
    await asyncio.sleep(2.5)

    result = await client.post(
        f"{base}/webhook/speech-result",
        data={
            "CallSid": sid,
            "SpeechResult": reason,
            "Confidence": f"{random.uniform(0.82, 0.98):.4f}",
        },
    )
    result.raise_for_status()
    print(f"[{sid}] transcript delivered, caller now on hold")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--calls", type=int, default=1)
    parser.add_argument("--say", help="what the caller says (default: a canned reason)")
    args = parser.parse_args()

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await asyncio.gather(
                *(
                    place_call(
                        client,
                        args.base_url.rstrip("/"),
                        args.say or REASONS[i % len(REASONS)],
                        delay=i * 1.5,
                    )
                    for i in range(args.calls)
                )
            )
    except (httpx.HTTPError, OSError) as exc:
        print(f"could not reach {args.base_url}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
