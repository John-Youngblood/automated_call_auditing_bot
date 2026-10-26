# Call Screener

A call-in queue for a live show. Callers say why they're calling and
wait on hold. Whoever has the dashboard open decides who goes on air.

## Why

Today the call-in number rings straight through to the studio phone. Whoever
is on the line when the crew picks up gets on air, and everyone else gets
voicemail. That means:

- **Calling in is a full-time job.** You hang up the moment voicemail starts,
  redial, and repeat for the whole episode. Listeners who tune in at work have
  almost no chance of getting through.
- **The crew picks up blind.** That leads to dead air, callers with no
  question ready, the same callers getting through again, and calls that are
  only there to praise the hosts.
- **Screening calls by hand would take someone's full attention**, so it has
  never happened.

This tool puts a queue between the callers and the studio phone:

1. A caller is asked what they want to talk about.
2. Twilio transcribes the answer and puts the caller on hold.
3. The dashboard shows everyone waiting: their number, what they said, and how
   long they've been holding.
4. **Accept** tells the caller they're through and rings the studio phone.
   **Reject** plays them a short message and hangs up.

Callers dial once and wait. The crew sees what each caller wants before
picking up, and they can check the queue between segments instead of
watching it all show. No AI decides who gets through. A person does.

```
   caller ──PSTN──▶ Twilio ──webhooks──▶ FastAPI ──websocket──▶ React dashboard
                       ▲                     │
                       └───── REST ──────────┘
                         (bridge / hang up)
```

Twilio handles the audio, including detecting when the caller has finished
talking, and sends the finished transcript in one webhook. Call history and
contacts are saved in SQLite on the machine running the app.

---

## Try it locally

You need **Docker**, or **Python 3.12 + Node 24** (both pinned in
`.tool-versions`). You don't need a Twilio account to try it.

```bash
cp .env.example .env
```

```bash
make up
```

- Dashboard → <http://localhost:5173>
- API docs → <http://localhost:8000/docs>

If a port is already taken, set `FRONTEND_PORT` / `BACKEND_PORT` in `.env`.

Put some fake callers in the queue:

```bash
make simulate CALLS=3
```

```bash
make simulate SAY="I have a question for your guest"
```

```bash
make simulate NUMBER=+15035550147   # run twice for a repeat caller
```

These send the same webhooks Twilio would, so the greeting, transcript and
queue all work without a phone. They are saved like real calls, so don't
point them at the office stack. History and contacts are kept in a Docker
volume (`backend/data/` without Docker), so they survive `make down`;
`docker compose down -v` starts fresh.

**Accept and Reject need Twilio.** They send real commands to Twilio, so
without credentials they return `503`. Even with credentials, Twilio doesn't
know about a simulated call, so accepting one fails. For a demo or a screen
recording, set `DEMO_MODE=true` (only allowed with `APP_ENV=local`). Accept,
Reject and closing the line then work on simulated calls without contacting
Twilio.

---

## Taking real calls

You need a Twilio account and two phone numbers:

- **`TWILIO_PHONE_NUMBER`**: the public number listeners call. You buy it in
  Twilio.
- **`HOST_PHONE_NUMBER`**: the studio phone that accepted callers are put
  through to. Keep it private. Anyone who has it can skip the queue.

**1. Make the app reachable.** Twilio connects over the internet, so the app
needs a public URL. While you're testing:

```bash
make tunnel
```

This starts a Cloudflare quick tunnel and writes its address into
`PUBLIC_BASE_URL`. `make url` prints that address.

**2. Point the Twilio number at the app.** In the Twilio Console, open Phone
Numbers → your number → Voice Configuration:

| Field | Value |
|---|---|
| A call comes in | `<base>/webhook/incoming-call` |
| Primary handler fails | a TwiML Bin saying "try again shortly" |
| Call status changes | `<base>/webhook/call-status` |

> ⚠️ A quick tunnel gets a **new hostname every time it restarts**, which
> breaks all three fields. Use one for testing only.

**3. Fill in `.env`:**

```bash
HOST_PHONE_NUMBER=+15035551234
TWILIO_PHONE_NUMBER=+15035559876
TWILIO_ACCOUNT_SID=AC…
TWILIO_API_KEY_SID=SK…
TWILIO_API_KEY_SECRET=…
TWILIO_AUTH_TOKEN=…          # webhook signature checks need this one
VALIDATE_WEBHOOK_SIGNATURE=true
```

### For an actual show

`make up` reloads whenever a file changes, which empties the queue. For a live
show, pick one of these setups:

- **[A machine in the office](docs/office.md)** (`make office`). Recommended.
  The app runs in Docker behind a named Cloudflare tunnel that keeps the same
  hostname, and anyone on the office network can open the dashboard. Any spare
  laptop works, or even a Raspberry Pi 4 or 5.
- **[Google Cloud](docs/deploy.md)**, if the dashboard needs to be reachable
  from outside the office.

With `APP_ENV=production`, the app won't start without a `DASHBOARD_PASSWORD`
and a real `HOST_PHONE_NUMBER`.

---

## Configuration

Settings go in `.env`. Every setting is explained in
[`.env.example`](.env.example). These are the ones you're most likely to
change.

### Cost and call handling

Twilio bills every minute a caller is on hold. That makes the size of the queue
and how long the line stays open the two numbers that decide the bill.

| Setting | Default | What it does |
|---|---|---|
| `HOLD_QUEUE_MAX_SIZE` | `200` | Most callers on hold at once. Anyone past it is dropped after giving their reason. |
| `MAX_HOLD_MINUTES` | `30` | Longest anyone can hold before hearing the reject message. `0` = no limit. |
| `LINE_OPEN_ON_START` | `true` | Whether the line is taking calls when the app starts. |
| `RECONCILE_ON_STARTUP` | `true` | After a restart, puts callers who are still on hold back on the dashboard. |
| `SPEECH_TIMEOUT_SECONDS` | `3` | Seconds of silence that count as "done talking". Raise it if callers get cut off. |
| `DASHBOARD_PASSWORD` | *(blank)* | Shared login for the dashboard. Blank means no login, which is allowed only locally. |

### What callers hear

Each prompt can be an **audio file**, with **text spoken in `TTS_VOICE`**
(default `Polly.Joanna`) as the fallback. If you set the audio, it plays;
otherwise the text is read out. This means the greeting can be a crew member
and the hold music can be anything you like.

| Moment | Audio | Spoken fallback |
|---|---|---|
| Greeting / reason for calling | `GREETING_AUDIO_URL` | `GREETING_MESSAGE` |
| Hold music | `HOLD_MUSIC_URL` | *(Twilio's default playlist)* |
| Accepted, before the studio phone rings | `ACCEPTED_AUDIO_URL` | `ACCEPTED_MESSAGE` |
| Rejected, or held past `MAX_HOLD_MINUTES` | `REJECT_AUDIO_URL` | `REJECT_MESSAGE` |
| On hold when the line closes | `CLOSING_AUDIO_URL` | `CLOSING_MESSAGE` |

An audio setting takes either a full `https://` URL or the bare filename of a
file in `backend/app/static/` (e.g. `GREETING_AUDIO_URL=greeting.mp3`).

Callers the show can't take hear nothing from you: anyone who calls while the
line is closed or the hold queue is full gets a busy signal, and a blocked
number or a caller whose caller ID fails Twilio's spoofing check hears "not in
service". These calls are never answered, so Twilio doesn't bill them — a flood
of calls costs nothing.

---

## During a show

- **Open / close the line** with the footer toggle. Closing it turns new
  callers away *and* hangs up on everyone still on hold, so nobody waits on a
  line no one is watching. Close it during ad reads or other stretches without
  calls to save money.
- **Clear out the queue now and then.** Rejecting callers you won't get to
  saves money even when you aren't ready to take a call.
- **Call History** lists finished calls, which ones made it on air, and for how
  long, searchable by what callers said, a name or a number. It's saved on the
  machine running the app, so restarts and updates keep it
  (`make office-backup` copies it off). What callers said is cleared after 90
  days (`TRANSCRIPT_RETENTION_DAYS`); the calls stay.
- **Contacts** give a number a name, a note, a favorite star, or a block.
  Callers in the queue show the name you saved and how often they've called,
  been on air or been rejected before. A blocked number hears "not in service"
  without ever reaching the queue, and blocking someone who's holding hangs up
  on them too (never on a caller who's on air).
- **Restarting is safe mid-show.** Callers still on hold come back on the
  dashboard with what they said. A restart also signs everyone out of the
  dashboard.

---

## Development

Without Docker:

```bash
make install
```

```bash
cd backend && .venv/bin/python -m uvicorn app.main:app --reload
```

```bash
cd frontend && npm run dev
```

Before you push, run the linter, the backend tests and the frontend typecheck:

```bash
make check
```

`make test-container` runs the backend tests in Docker, on the SQLite version
the containers ship, which is older than a Mac's.

`make help` lists everything else. [docs/architecture.md](docs/architecture.md)
explains where to start reading the code and why it's built the way it is.

---

## Future features

| Feature | Description |
|---|---|
| VoIP call-ins | Let listeners call from a browser or app instead of dialing a phone number. Twilio charges less per minute for these calls than for calls over the phone network, and holding is most of the bill. |
| HD audio | Phone-network audio is narrowband. A caller who comes in over VoIP could reach the studio in wideband audio, but only if the studio end is VoIP too (a browser or SIP client instead of a phone). Depends on VoIP call-ins. |
