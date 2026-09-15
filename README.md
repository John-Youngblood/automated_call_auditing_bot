# Call Screener

Screens inbound phone calls. A caller is greeted, asked why they're calling,
and put on hold; their transcribed reason appears on a dashboard where an
operator accepts, rejects, or blocks them.

```
   caller
     │  PSTN
     ▼
┌──────────┐  POST /webhook/incoming-call    ┌─────────────────────┐
│          │ ──────────────────────────────▶ │                     │
│  Twilio  │ ◀────────────────────────────── │      FastAPI        │
│          │   <Gather input="speech">       │                     │
│          │                                 │  ┌───────────────┐  │
│  greeting plays, Twilio listens,           │  │   blocklist   │  │
│  detects when the caller stops             │  │  call history │  │
│          │                                 │  │   (SQLite)    │  │
│          │  POST /webhook/speech-result    │  └───────┬───────┘  │
│          │ ──────────────────────────────▶ │          │          │
│          │ ◀────────────────────────────── │          │          │
│          │   <Enqueue> (hold music)        │          │          │
└──────────┘                                 └──────────┼──────────┘
                                                        │ WS /ws/frontend
                                                        ▼
                                               ┌──────────────────┐
                                               │ React dashboard  │
                                               │ queue + history  │
                                               └──────────────────┘
```

**Twilio owns the hard part** — deciding when the caller has stopped talking —
which is why this service handles no audio at all. No media streaming, no
speech-to-text integration, no audio buffering. The transcript arrives complete
in one webhook.

## Quick start

```bash
cp .env.example .env
docker compose up --build
```

- Dashboard: <http://localhost:5173>
- API docs: <http://localhost:8000/docs>

No credentials needed to try it. Place some fake calls:

```bash
make simulate CALLS=3
```

Each appears in the queue with its transcript, and Accept / Reject / Block
resolve them.

> If port 5173 or 8000 is taken, set `FRONTEND_PORT` / `BACKEND_PORT` in `.env`.

## The call flow

| # | Endpoint | Returns | Caller hears |
|---|---|---|---|
| 1 | `POST /webhook/incoming-call` | `<Gather input="speech">` with the greeting nested inside | the greeting, then silence while Twilio listens |
| 2 | `POST /webhook/speech-result` | `<Enqueue>` | hold music |
| 3 | `POST /webhook/call-status` | — | (call is over) |

Three details that matter:

- **`speechTimeout="auto"`** is what makes this turn-based. Twilio decides when
  the caller stopped and only then posts the transcript.
- **`<Play>` sits inside `<Gather>`**, so the greeting doubles as the prompt and
  a caller who talks over it is still heard.
- **`<Enqueue>`** holds the call open with Twilio's built-in hold music — no
  queue to pre-create, no hold audio to host, no redirect loop to maintain.

A blocked caller never gets past step 1: they're refused with `<Reject>`, which
drops the call before it's answered, so there's no answered leg and no
per-minute charge.

## Layout

```
.
├── docker-compose.yml        # both services, one command
├── Makefile                  # make help
├── .env.example              # every knob, documented
├── backend/
│   ├── app/
│   │   ├── main.py           # composition root
│   │   ├── config.py         # typed settings, the only reader of the environment
│   │   ├── api/routes/
│   │   │   ├── webhooks.py       # the three Twilio webhooks
│   │   │   ├── calls.py          # accept / reject
│   │   │   ├── moderation.py     # block-number / call-history
│   │   │   ├── frontend.py       # WS /ws/frontend
│   │   │   └── health.py
│   │   ├── services/
│   │   │   ├── call_registry.py  # single writer of call state
│   │   │   ├── broadcaster.py    # bounded-queue fan-out to dashboards
│   │   │   ├── blocklist.py      # cached blocklist, read on every ring
│   │   │   ├── call_history.py   # durable record of finished calls
│   │   │   └── phone.py          # E.164 normalisation
│   │   ├── db/                   # SQLAlchemy models + async session
│   │   └── telephony/            # Twilio-specific code, all of it
│   │       ├── twiml.py              # the three XML documents
│   │       ├── signature.py          # webhook authenticity
│   │       └── provider_client.py    # REST control: hangup / bridge (stub)
│   ├── scripts/simulate_call.py  # fake calls, no phone needed
│   └── tests/
└── frontend/src/
    ├── components/           # presentational only
    ├── hooks/
    │   ├── useCallStream.ts      # one socket, one reducer
    │   ├── useCallHistory.ts     # history is a plain HTTP read
    │   └── useBlockNumber.ts     # the block flow, shared by both views
    └── types/events.ts       # mirrors backend/app/schemas/events.py
```

## Moderation

### Blocking

`POST /api/block-number` does two things that must not be confused:

1. adds the number to `blocked_numbers`, so **future** calls are refused at the
   webhook; and
2. hangs up whatever that caller has in flight **right now**, via Twilio's REST
   API.

The first is durable and cheap. The second is a request to a third party and
can fail, so the response reports them separately — `terminatedCallIds` vs
`failedCallIds` — and the dashboard says "blocked, but we could not drop the
live call" rather than letting silence imply success.

Blocked attempts are still written to `call_history`. A block that hides
evidence of repeat harassment is worse than no record at all.

**Numbers are normalised before anything compares them.** `+1 (555) 019-2834`,
`555-019-2834` and `+15550192834` all reach the same key — see
[phone.py](backend/app/services/phone.py). This is the detail the whole feature
rests on: get it wrong and blocking silently does nothing.

### Call history

`GET /api/call-history` returns finished calls newest-first — completed,
rejected, dropped and blocked — each with a transcript summary. The dashboard
shows it in a separate tab from the live queue: the queue is a work surface
where seconds matter, history is a record read at leisure.

Every history row carries a Block action, because by the time you decide
someone needs blocking the call is usually already over.

## Going live

1. Expose the backend publicly — Twilio dials in from the internet:
   ```bash
   ngrok http 8000
   ```
2. Set `PUBLIC_BASE_URL` to that origin. It builds the greeting URL and the
   `action` URL on `<Gather>`, so `localhost` will not work.
3. Point your number's voice webhook at `/webhook/incoming-call` and its status
   callback at `/webhook/call-status`.
4. Set `TWILIO_AUTH_TOKEN` and `VALIDATE_WEBHOOK_SIGNATURE=true`. Both webhooks
   are public URLs; unsigned, anyone who finds them can fabricate a call or
   inject words the caller never said.
5. Replace the silent placeholder greeting
   (`backend/app/static/greeting.mp3`) with a real recording that asks the
   caller to state their reason, or set `GREETING_AUDIO_URL`.

`SPEECH_MODEL` defaults to `phone_call`, which is tuned for 8kHz telephony
audio. The default model is trained on wideband and does noticeably worse down
a phone line.

## Development without Docker

```bash
make install
cd backend && .venv/bin/python -m uvicorn app.main:app --reload
cd frontend && npm run dev
```

Node 24 and Python 3.12 are pinned in `.tool-versions`.

```bash
make check      # ruff + pytest + tsc
```

## Design notes

**One writer of call state.** Every mutation goes through `CallRegistry`, and
every mutation publishes its event, so no code path can change a call without
the dashboards hearing about it.

**The dashboard can't slow anything down.** Each connected dashboard gets a
bounded queue; publishing uses `put_nowait` and drops the *oldest* event when a
client falls behind. A backgrounded browser tab slows only itself.

**The blocklist is read from memory.** It's consulted on every ring while
Twilio holds the caller waiting, so it's a cached set loaded at startup, not a
query.

**Single worker, on purpose.** Call state and the fan-out hub are in-process.
See [docs/architecture.md](docs/architecture.md) for the scale-out path.

## What is still a placeholder

| Area | State |
| --- | --- |
| Accept / Reject / Block hang-up | State changes and broadcasts are real; every Twilio REST command (bridge, decline, hangup) is a logged stub in [provider_client.py](backend/app/telephony/provider_client.py) |
| Greeting MP3 | Valid but silent |
| Unblocking | No way to remove a number except by editing the database |
| Auth | No login on the dashboard, no authorisation on the API — `blockedBy` is therefore unverified |
| Providers | Twilio only. `<Gather input="speech">` has no direct equivalent elsewhere, so another provider means a real port, not a config change |
