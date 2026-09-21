# H3 Call Screener

![h3_logo.png](frontend/public/h3_logo.png)

Screens inbound phone calls for the H3 live podcast. A caller is greeted, asked why
they're calling, and put on hold. Their transcribed reason appears on a
dashboard where an operator puts them on air or turns them away.

```
   caller ──PSTN──▶ Twilio ──webhooks──▶ FastAPI ──websocket──▶ React dashboard
                       ▲                     │
                       └───── REST ──────────┘
                         (bridge / hang up)
```

**Twilio owns the hard part** — deciding when the caller has stopped talking —
so this service handles no audio. No media streaming, no speech-to-text
integration. The transcript arrives complete, in one webhook.

**Nothing is stored on disk.** No database. Calls being screened and the last
few hundred finished ones live in one in-memory registry.

---

## Run it locally

You need **Docker**, or **Python 3.12 + Node 24** (both pinned in
`.tool-versions`). No Twilio account needed to try it.

```bash
cp .env.example .env
```

```bash
make up
```

- Dashboard → <http://localhost:5173>
- API docs → <http://localhost:8000/docs>

> Port taken? Set `FRONTEND_PORT` / `BACKEND_PORT` in `.env`.

Now put some fake callers in the queue:

```bash
make simulate CALLS=3
```

```bash
make simulate SAY="I have a question for your guest"
```

`simulate_call.py` posts the same webhooks Twilio would, so the whole flow —
greeting, transcript, queueing — runs without a phone. Each call appears in the
dashboard with its transcript.

**Accept and Reject need Twilio credentials.** They issue real REST commands,
so without `TWILIO_ACCOUNT_SID` and an API key they return `503` rather than
pretending to work. Everything else works offline.

### Running it for a real show

The development stack above reloads on every file save, which empties the
queue. For an actual show, run the built images on a machine in the office and
let everyone else open it over the LAN:

```bash
make office
```

Three containers: nginx serving the dashboard, the backend, and a Cloudflare
tunnel so Twilio can reach it without touching the router. Colleagues need
nothing installed. See **[docs/office.md](docs/office.md)** for the one-time
setup, or **[docs/deploy.md](docs/deploy.md)** to run it on Google Cloud
instead.

### Without Docker

```bash
make install
```

```bash
cd backend && .venv/bin/python -m uvicorn app.main:app --reload
```

```bash
cd frontend && npm run dev
```

### Before you push

```bash
make check
```

Ruff, the backend test suite, and a frontend typecheck. `make help` lists
everything else.

---

## Taking a real call

Twilio dials in from the internet, so it needs a public URL.

**1. Open a tunnel.** This starts `cloudflared`, rewrites `PUBLIC_BASE_URL` in
`.env`, and recreates the backend so it picks the new hostname up:

```bash
make tunnel
```

**2. Point your Twilio number at it.** Console → Phone Numbers → your number →
Voice Configuration. `make url` prints the base.

| Field | Value |
|---|---|
| A call comes in | `<base>/webhook/incoming-call` |
| Primary handler fails | a TwiML Bin saying "try again shortly" |
| Call status changes | `<base>/webhook/call-status` |

The other webhooks aren't configured here — their URLs are baked into the TwiML
this service returns at each step.

> ⚠️ A quick tunnel's hostname **rotates on every restart**, and all three
> fields go stale together. `make tunnel` updates `.env` but can't update
> Twilio. For anything beyond a test, use a named tunnel or a real deploy.

**3. Set the host's phone**, or accepted callers get dialled nowhere:

```bash
HOST_PHONE_NUMBER=+15035551234
```

The service refuses to start on the placeholder outside `APP_ENV=local`.

**4. Add Twilio credentials** so Accept, Reject and closing the line work:

```bash
TWILIO_ACCOUNT_SID=AC…
TWILIO_API_KEY_SID=SK…
TWILIO_API_KEY_SECRET=…
TWILIO_AUTH_TOKEN=…          # signature checks need this one specifically
VALIDATE_WEBHOOK_SIGNATURE=true
```

Both webhooks are public URLs. Unsigned, anyone who finds them can fabricate a
call or put words in a caller's mouth.

---

## Reading the code

Start at `backend/app/api/routes/webhooks.py` — the whole call flow is four
handlers, in order:

| Endpoint | Returns | Caller hears |
|---|---|---|
| `incoming-call` | `<Gather>` with the greeting inside | the greeting, then silence while Twilio listens |
| `speech-result` | `<Enqueue>` | hold music |
| `queue-exit` | empty | *(they left the queue)* |
| `dial-complete` | `<Hangup>` | *(their time with the host ended)* |

Then:

- `telephony/twiml.py` — the XML documents, one function each
- `services/call_registry.py` — the single writer of call state
- `services/decisions.py` — accept and reject
- `frontend/src/hooks/useCallStream.ts` — one socket, one reducer

**One rule explains most of the design:** the dashboard must never claim
something the phone line didn't do. Twilio acts first, local state follows, and
a command that fails leaves the call visible rather than quietly resolved.

`docs/architecture.md` has the why — the trade-offs, the failure modes, and
what to change when the assumptions stop holding. `docs/deploy.md` covers
running it on Google Cloud.

---

## What the caller hears

Six moments, each an **audio file with a spoken fallback**. Set the audio and
it plays; otherwise Twilio speaks the text in `TTS_VOICE`.

| Moment | Audio | Fallback |
|---|---|---|
| Greeting — *also the prompt* | `GREETING_AUDIO_URL` | `GREETING_MESSAGE` |
| Hold music | `HOLD_MUSIC_URL` | *(Twilio's playlist)* |
| Rejected | `REJECT_AUDIO_URL` | `REJECT_MESSAGE` |
| Line closed | `CLOSED_LINE_AUDIO_URL` | `CLOSED_LINE_MESSAGE` |
| Closing the line | `CLOSING_AUDIO_URL` | `CLOSING_MESSAGE` |
| Accepted | *(dialled to `HOST_PHONE_NUMBER`)* | — |

Audio settings take an absolute URL **or a bare filename** served from
`backend/app/static/`. Use the filename in development — the tunnel hostname
rotates and `.env` can't interpolate it. Use a CDN in production.

---

## Using it during a show

**On air / off air.** The footer toggles the line. Closing it turns new callers
away *and* hangs up on anyone still holding — one action, so nobody is left
waiting on a line nobody is watching. It asks first.

**Call History** shows finished calls, and marks which ones actually made it on
air and for how long. It's in memory, so it clears when the backend restarts.

**Signing in.** One shared `DASHBOARD_PASSWORD`, traded for a session token.
No accounts, no reset flow. Tokens are held in memory, so a restart signs
everyone out — which is also how you revoke one. Leave the password blank
locally and the dashboard is open; anywhere else the service refuses to start
without it.

**A restart mid-show doesn't strand callers.** On boot the service asks Twilio
who is still in the hold queue and puts them back on the dashboard. Their
transcript can't be recovered — it only ever existed in memory — so those rows
say so rather than looking like a caller who stayed silent.

---

## Not done yet

| | |
|---|---|
| Per-user auth | One shared password, so there is no "who rejected that caller". No rate limiting on guesses either — the length floor is the only defence |
| One on-air slot | Accepting a second caller while one is live dials a busy host. Nothing prevents it; the outcome is reported honestly |
| Caller names | Needs Caller ID Lookup on the number (paid, off by default) |
| Blocking / favourites | No way to bar a repeat troll or flag a good caller. Needs E.164 normalisation back (it was removed with the blocklist) and somewhere durable to keep the list — a blocklist that empties on deploy is not a blocklist |
| Persistent call history | History lives in memory, capped at `CALL_HISTORY_SIZE` and cleared on restart. A database would also carry the transcript through a restart, which is the one thing reconciliation cannot recover |
| Single worker | Call state and dashboard fan-out are in-process — see `docs/architecture.md` |
