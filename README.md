# Call Screener

Screens inbound phone calls. A caller is greeted, asked why they're calling,
and put on hold; their transcribed reason appears on a dashboard where an
operator accepts or rejects them.

```
   caller
     │  PSTN
     ▼
┌──────────┐  POST /webhook/incoming-call    ┌─────────────────────┐
│          │ ──────────────────────────────▶ │                     │
│  Twilio  │ ◀────────────────────────────── │      FastAPI        │
│          │   <Gather input="speech">       │                     │
│          │                                 │  ┌───────────────┐  │
│  greeting plays, Twilio listens,           │  │ CallRegistry  │  │
│  detects when the caller stops             │  │ queue+history │  │
│          │                                 │  │  (in memory)  │  │
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

**Nothing is stored on disk.** There is no database. Calls being screened and
the last few hundred finished ones live in one in-memory registry, so a restart
starts with an empty history — see [Call history](#call-history).

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
make simulate SAY="I have a question for your guest"   # pick the words
```

Each appears in the queue with its transcript, and Accept / Reject resolve
them.

> If port 5173 or 8000 is taken, set `FRONTEND_PORT` / `BACKEND_PORT` in `.env`.

## The call flow

| # | Endpoint | Returns | Caller hears |
|---|---|---|---|
| 1 | `POST /webhook/incoming-call` | `<Gather input="speech">` with the greeting nested inside | the greeting, then silence while Twilio listens |
| 2 | `POST /webhook/speech-result` | `<Enqueue>` | hold music |
| 3 | `POST /webhook/queue-exit` | empty `<Response/>` | (they left the queue) |
| 4 | `POST /webhook/call-status` | `204` | (call is over) |

Four details that matter:

- **`speechTimeout` is a number, never `"auto"`.** It's what makes this
  turn-based — Twilio decides when the caller stopped and only then posts the
  transcript. Not `auto` for two reasons: Twilio warns (error 13335) when
  `auto` is combined with a `speechModel`, and `auto` stops at the *first*
  pause, truncating anyone mid-explanation.
- **`<Play>` sits inside `<Gather>`**, so the greeting doubles as the prompt and
  a caller who talks over it is still heard.
- **`<Enqueue>`** holds the call open with Twilio's built-in hold music — no
  queue to pre-create, no hold audio to host, no redirect loop to maintain.
- **`<Enqueue action>` is how abandonment is detected.** Nothing keeps a
  connection to this service while a caller holds, so without it a caller who
  gives up leaves no trace. Twilio posts `QueueResult=hangup` and `QueueTime`.

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
│   │   │   ├── webhooks.py       # the four Twilio webhooks
│   │   │   ├── calls.py          # accept / reject / call-history
│   │   │   ├── frontend.py       # WS /ws/frontend
│   │   │   └── health.py
│   │   ├── services/
│   │   │   ├── call_registry.py  # single writer of call state, live + recent
│   │   │   ├── broadcaster.py    # bounded-queue fan-out to dashboards
│   │   │   └── phone.py          # caller location labels
│   │   └── telephony/            # Twilio-specific code, all of it
│   │       ├── twiml.py              # the two XML documents
│   │       ├── signature.py          # webhook authenticity
│   │       └── provider_client.py    # REST control: bridge / decline (stub)
│   ├── scripts/simulate_call.py  # fake calls, no phone needed
│   └── tests/
└── frontend/src/
    ├── components/           # presentational only
    ├── hooks/
    │   ├── useCallStream.ts      # one socket, one reducer
    │   └── useCallHistory.ts     # history is a plain HTTP read
    └── types/events.ts       # mirrors backend/app/schemas/events.py
```

## Call history

`GET /api/call-history` returns finished calls newest-first — accepted,
rejected and dropped. It reads straight out of `CallRegistry`, which keeps the
last `CALL_HISTORY_SIZE` (default 200) alongside the live ones, so it serves
the same `Call` shape as the queue: one wire type for a call wherever it
appears.

The dashboard shows it in a separate tab from the live queue: the queue is a
work surface where seconds matter, history is a record read at leisure.

**It is in memory, so a restart clears it.** That is the trade for having no
database, and it is the right one at this size — a screening decision is made
within seconds, and the alternative was a SQLite file whose only reader was a
list of recent calls. If you later need history to survive a deploy, that is
the moment to add storage back, not before.

## Going live

1. Expose the backend publicly — Twilio dials in from the internet:
   ```bash
   ngrok http 8000
   ```
2. Set `PUBLIC_BASE_URL` to that origin. It builds the greeting URL and the
   `action` URL on `<Gather>`, so `localhost` will not work.
3. Point your number's voice webhook at `/webhook/incoming-call`.
4. Point the number's **status callback** at `/webhook/call-status`. This one
   is easy to skip and costly to skip: nothing else tells this service that a
   caller hung up while on hold, so without it their card sits in the queue
   until someone tries to put a dead line on air.
5. Set `TWILIO_AUTH_TOKEN` and `VALIDATE_WEBHOOK_SIGNATURE=true`. Both webhooks
   are public URLs; unsigned, anyone who finds them can fabricate a call or
   inject words the caller never said.
6. Optionally enable **Caller ID Lookup** (`VoiceCallerIdLookup`) on the
   number if you want caller names. It is off by default and billed per
   lookup; without it `CallerName` is never sent and every caller shows as a
   bare number.
7. Check the greeting (`backend/app/static/greeting.mp3`) still says what you
   want. It plays inside `<Gather>`, so it *is* the prompt — it has to ask the
   caller to state their reason. Set `GREETING_AUDIO_URL` to host it
   elsewhere.

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

**No persistence at all.** The webhook path touches no disk, so nothing can
put a slow write in front of a ringing phone. It also means there is no schema,
no migration story and no volume to manage.

**Single worker, on purpose.** Call state and the fan-out hub are in-process,
so a second worker would see a different set of calls. See
[docs/architecture.md](docs/architecture.md) for the scale-out path.

## What is still a placeholder

| Area | State |
| --- | --- |
| Accept / Reject | State changes and broadcasts are real; both Twilio REST commands (bridge, decline) are logged stubs in [provider_client.py](backend/app/telephony/provider_client.py), so Accept does not yet put anyone on air |
| Caller names | Requires Caller ID Lookup enabled on the number (paid, off by default). Without it every caller is a bare number |
| Auth | No login on the dashboard and no authorisation on the API |
| Providers | Twilio only. `<Gather input="speech">` has no direct equivalent elsewhere, so another provider means a real port, not a config change |
