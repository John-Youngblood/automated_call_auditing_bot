# Call Screener

Real-time phone call screening. A telecom provider streams live call audio to a
FastAPI backend, which transcribes it through Deepgram and pushes the text to a
React dashboard as the caller speaks. An operator watches the transcript and
decides whether to take the call.

```
   caller
     │  PSTN
     ▼
┌──────────────┐  POST /webhook/incoming-call   ┌──────────────────────┐
│   telecom    │ ─────────────────────────────▶ │                      │
│   provider   │ ◀───────────────────────────── │       FastAPI        │
│ (Twilio etc) │   TwiML / NCCO: play + stream  │       backend        │
│              │                                │                      │
│              │  WS /ws/audio-stream           │   ┌──────────────┐   │
│              │ ═══ 20ms audio frames ═══════▶ │   │  screening   │   │
└──────────────┘                                │   │   session    │   │
                                                │   └──────┬───────┘   │
                                                │          ▼           │
                                                │   ┌──────────────┐   │
                                                │   │  Deepgram    │   │
                                                │   │  streaming   │   │
                                                │   └──────┬───────┘   │
                                                │          ▼           │
                                                │   ┌──────────────┐   │
                                                │   │ registry +   │   │
                                                │   │ broadcaster  │   │
                                                └───┴──────┬───────┴───┘
                                                           │ WS /ws/frontend
                                                           ▼
                                                  ┌──────────────────┐
                                                  │ React dashboard  │
                                                  │ queue+transcript │
                                                  └──────────────────┘
```

## Quick start

```bash
cp .env.example .env
docker compose up --build
```

- Dashboard: <http://localhost:5173>
- API docs: <http://localhost:8000/docs>

Speech-to-text defaults to **mock mode**, so the stack runs end to end with no
credentials. Place some fake calls against it:

```bash
make simulate CALLS=3 SECONDS=20
```

Calls appear in the queue with a live transcript, and the Accept / Reject
buttons resolve them.

> If port 5173 or 8000 is already taken, set `FRONTEND_PORT` / `BACKEND_PORT`
> in `.env`.

## Layout

```
.
├── docker-compose.yml        # both services, watch mode, one command
├── Makefile                  # make help
├── .env.example              # every knob, documented
├── backend/
│   ├── app/
│   │   ├── main.py           # composition root: builds singletons, mounts routers
│   │   ├── config.py         # typed settings, the only reader of the environment
│   │   ├── api/routes/       # transport only, no business logic
│   │   │   ├── webhooks.py       # POST /webhook/incoming-call
│   │   │   ├── audio_stream.py   # WS   /ws/audio-stream   (provider audio in)
│   │   │   ├── frontend.py       # WS   /ws/frontend       (dashboard events out)
│   │   │   ├── calls.py          # REST accept / reject
│   │   │   ├── moderation.py     # REST block-number / call-history
│   │   │   └── health.py
│   │   ├── services/         # state and integrations
│   │   │   ├── broadcaster.py    # bounded-queue pub/sub to dashboards
│   │   │   ├── call_registry.py  # single writer of call state
│   │   │   ├── screening.py      # per-call orchestration, audio → STT
│   │   │   ├── deepgram.py       # streaming STT (real + mock)
│   │   │   ├── blocklist.py      # cached blocklist, read on every ring
│   │   │   ├── call_history.py   # durable record of finished calls
│   │   │   └── phone.py          # E.164 normalisation (blocking hinges on it)
│   │   ├── db/              # SQLAlchemy models + async session
│   │   ├── telephony/        # everything provider-specific
│   │   │   ├── twiml.py / ncco.py    # XML and JSON response dialects
│   │   │   ├── media_stream.py       # frame parsing, outbound audio
│   │   │   ├── provider_client.py    # REST control: hangup / bridge (stub)
│   │   │   └── signature.py          # webhook authenticity
│   │   └── schemas/          # wire contracts
│   ├── scripts/simulate_call.py  # fake calls, no phone needed
│   └── tests/
└── frontend/
    └── src/
        ├── components/       # presentational only
        │   ├── CallHistoryView.tsx  # past calls + retroactive block
        │   └── ConfirmDialog.tsx    # native <dialog>, focus starts on Cancel
        ├── hooks/
        │   ├── useCallStream.ts     # one socket, one reducer
        │   ├── useCallHistory.ts    # history is a plain HTTP read
        │   └── useBlockNumber.ts    # the block flow, shared by both views
        ├── lib/websocket.ts        # reconnect + heartbeat
        └── types/events.ts         # mirrors backend/app/schemas/events.py
```

## The three endpoints

### `POST /webhook/incoming-call`

The provider calls this the moment a phone rings and waits a couple of seconds
for instructions. The response answers the call, plays the MP3 greeting, and
opens a two-way media stream:

```xml
<Response>
  <Play>https://your-host/static/greeting.mp3</Play>
  <Connect>
    <Stream url="wss://your-host/ws/audio-stream">
      <Parameter name="callId" value="CAxxxx"/>
    </Stream>
  </Connect>
</Response>
```

Set `TELEPHONY_PROVIDER=vonage` to get the equivalent as JSON (an NCCO) instead.

Order matters: `<Connect>` blocks until the stream ends, so the greeting has to
be queued before it. And `<Connect><Stream>` is the two-way verb —
`<Start><Stream>` is a one-way fork that cannot play audio back.

### `WS /ws/audio-stream`

Where the provider streams the caller's voice, as JSON text frames with base64
audio inside (`connected` → `start` → `media`×N → `stop`), about 50 frames per
second per call. The route is a thin pump; each frame's audio is handed to
`CallScreeningSession.feed_audio`, which is where transcription begins.

The reverse direction is stubbed: `play_to_caller()` in
`services/screening.py` sends audio back down the same socket, ready for hold
messages or TTS.

### `WS /ws/frontend`

Broadcasts call state and transcript lines to every dashboard, and accepts
`ping` / `call.accept` / `call.reject` back. On connect it immediately sends a
full snapshot, so a dashboard opened mid-call is never blank.

## Moderation

### Blocking

`POST /api/block-number` does two things that must not be confused:

1. adds the number to `blocked_numbers`, so **future** calls are refused at the
   webhook before any stream opens; and
2. hangs up whatever that caller has in flight **right now**, through the
   provider's REST API.

The first is durable and cheap. The second is a request to a third party and
can fail, so the response reports them separately —
`terminatedCallIds` vs `failedCallIds` — and the dashboard says "blocked, but
we could not drop the live call" rather than letting silence imply success.

A blocked caller is refused with `<Reject>` (TwiML), which drops the call at the
carrier: no answered leg, no media stream, no Deepgram session, no per-minute
charge. Answering and then hanging up would cost all four. Set
`BLOCKED_CALL_REJECT_REASON=busy` to return a busy signal instead, which looks
like an ordinary failed call rather than a deliberate block.

Blocked attempts are still written to `call_history`. A block that hides
evidence of repeat harassment is worse than no record at all.

**Numbers are normalised before anything compares them.** `+1 (555) 019-2834`,
`555-019-2834` and `+15550192834` all reach the same key — see
`backend/app/services/phone.py`. This is the detail the whole feature rests on:
get it wrong and blocking silently does nothing.

### Call history

`GET /api/call-history` returns finished calls newest-first: completed,
rejected, dropped and blocked, each with a transcript summary. The dashboard
shows it in a separate tab from the live queue — the queue is a work surface
where seconds matter, history is a record read at leisure.

Every history row carries a Block action, because by the time you decide
someone needs blocking the call is usually already over.

## Wiring up a real provider

1. Expose the backend publicly — providers dial in from the internet:
   ```bash
   ngrok http 8000
   ```
2. Put that origin in `.env` as `PUBLIC_BASE_URL` (it is what builds the
   `wss://` stream URL and the greeting URL).
3. Point your phone number's voice webhook at
   `https://<your-tunnel>/webhook/incoming-call`, and its status callback at
   `/webhook/call-status`.
4. Set `TWILIO_AUTH_TOKEN` and `VALIDATE_WEBHOOK_SIGNATURE=true`. The webhook is
   a public URL; unsigned, anyone who finds it can fabricate calls.
5. Replace the silent placeholder greeting (`backend/app/static/greeting.mp3`)
   with a real recording, or set `GREETING_AUDIO_URL`.

### Turning on Deepgram

```env
DEEPGRAM_API_KEY=your-key
STT_MOCK=false
```

The audio format must match what the provider sends or you get confident
nonsense back. Twilio Media Streams are mu-law 8kHz mono (the default here);
Vonage websockets are linear16 at 16kHz.

## Development without Docker

```bash
make install                                  # venv + npm install
cd backend && .venv/bin/uvicorn app.main:app --reload
cd frontend && npm run dev
```

Node 24 and Python 3.12 are pinned in `.tool-versions`.

```bash
make check      # ruff + pytest + tsc
make test
```

## Design notes

**The audio path never waits on a dashboard.** Every dashboard gets a bounded
queue; publishing uses `put_nowait` and drops the *oldest* event when a client
falls behind. A backgrounded browser tab slows only itself, and a slow reader
converges on "now" rather than replaying history.

**Audio is buffered before transcription** for the same reason. Frames arrive
on a hard 20ms clock, so `feed_audio` enqueues and returns; a separate pump task
talks to Deepgram. A stalled STT upstream degrades the call instead of stalling
frame reads.

**One writer of call state.** Every mutation goes through `CallRegistry`, and
every mutation publishes its event, so no code path can change a call without
the dashboards hearing about it.

**Single worker, on purpose.** Call state and the fan-out hub are in-process, so
a second uvicorn worker would see a different set of calls. See
[docs/architecture.md](docs/architecture.md) for the scale-out path.

## What is still a placeholder

| Area | State |
| --- | --- |
| Accept / Reject / Block hang-up | State changes and broadcasts are real; every provider-side command (bridge, decline, hangup) is a logged stub in `telephony/provider_client.py` |
| Unblocking | There is no way to remove a number from the blocklist except by editing the database |
| Deepgram client | Written against the documented protocol but not yet exercised against the live API; mock mode is the default |
| Outbound audio | `play_to_caller()` sends correctly-framed audio; nothing generates any yet |
| Greeting MP3 | Valid but silent |
| Auth | No login on the dashboard, no authorisation on the API — `blockedBy` is therefore unverified |
