#!/usr/bin/env python3
"""Generate app/static/greeting.mp3 -- a valid but SILENT placeholder.

Exists so ``<Play>`` in the webhook response resolves to a real file during
local development instead of 404ing at the provider. It is silence: replace it
with an actual recording, or point GREETING_AUDIO_URL at a hosted file.

    python scripts/make_placeholder_greeting.py --seconds 3

Why hand-assembled: macOS ships no MP3 *encoder* (afconvert decodes only), and
pulling ffmpeg or lame in just to make two seconds of nothing is not worth it.
An MPEG-1 Layer III frame whose main data is all zeros is well-formed and
decodes to silence, so the frames are emitted directly.
"""

from __future__ import annotations

import argparse
from pathlib import Path

# MPEG-1 Layer III, 128 kbps, 44.1 kHz, mono, no CRC.
#   FF FB -> sync + MPEG1 + Layer III + no protection
#   90    -> bitrate index 9 (128 kbps), 44.1 kHz, no padding
#   C4    -> mono, not copyrighted, original
FRAME_HEADER = bytes((0xFF, 0xFB, 0x90, 0xC4))

# floor(144 * bitrate / sample_rate) = floor(144 * 128000 / 44100)
FRAME_SIZE = 417
SAMPLES_PER_FRAME = 1152
SAMPLE_RATE = 44100


def build_silent_mp3(seconds: float) -> bytes:
    frame = FRAME_HEADER + bytes(FRAME_SIZE - len(FRAME_HEADER))
    frame_count = max(1, round(seconds * SAMPLE_RATE / SAMPLES_PER_FRAME))
    return frame * frame_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "app" / "static" / "greeting.mp3",
    )
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    data = build_silent_mp3(args.seconds)
    args.out.write_bytes(data)
    print(f"wrote {args.out} ({len(data)} bytes, ~{args.seconds:g}s of silence)")


if __name__ == "__main__":
    main()
