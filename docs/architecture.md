# Architecture notes

Companion to the README: why the structure is what it is, and what to change
when the assumptions stop holding.

## Layout

```
backend/app/
  main.py                composition root: builds the singletons, wires routes
  config.py              the only reader of the environment
  api/
    deps.py              app.state -> route dependencies
    routes/webhooks.py   the four Twilio webhooks, in call order
    routes/calls.py      accept / reject / history / line open+close
    routes/contacts.py   name, note, favorite, block
    routes/frontend.py   WS /ws/frontend
  services/
    call_registry.py     single writer of call state; saves every change
    store.py             SQLite: call history and contacts
    migrations.py        the schema, append-only
    broadcaster.py       bounded-queue fan-out to dashboards
    decisions.py         accept and reject, carrier-first
    drain.py             hang up on holders when the line closes
    line_state.py        on air / off air, broadcast to dashboards
    reconcile.py         rebuild the queue from Twilio on boot
    phone.py             which numbers are real, location labels
  telephony/             everything Twilio-specific
    twiml.py             the five XML documents
    rest.py              the four REST endpoints we call
    signature.py         webhook authenticity
  static/                audio files the settings name, served at /static

frontend/src/
  hooks/useCallStream.ts one socket, one reducer
  hooks/useCallHistory.ts history is a paged HTTP read
  components/            presentational only
  types/events.ts        mirrors backend/app/schemas/
```

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

## The call flow, and why it is shaped that way

Four details are load-bearing and easy to undo by accident:

- **`<Play>` sits inside `<Gather>`**, so the greeting doubles as the prompt
  and Twilio is already listening as it finishes. A caller who talks over the
  greeting is still heard.
- **`speechTimeout` is a number, never `"auto"`.** It is what makes this
  turn-based: Twilio decides when the caller stopped and posts the finished
  transcript. Not `auto` for two reasons — Twilio warns (error 13335) when it
  is combined with a `speechModel`, and `auto` stops at the *first* pause,
  which truncates anyone mid-explanation. Fine for "say your account number",
  wrong for "tell us why you are calling".
- **`<Enqueue action>` is the only report of abandonment.** Nothing keeps a
  connection to this service while a caller holds, so without it a caller who
  gives up leaves no trace and their card sits on the dashboard until someone
  tries to put a dead line on air.
- **`<Dial action>` is the only report of the on-air outcome**, and it changes
  `<Dial>`'s behaviour: instead of ending the call when the dial finishes,
  Twilio keeps the *caller's* leg alive and hands control back. Whatever serves
  that URL must hang up, or a caller sits connected to silence after the host
  hangs up.

`actionOnEmptyResult` and the trailing `<Redirect>` cover the same risk from
both sides: a silent caller still reaches the dashboard, and a `<Gather>` that
falls through lands back on the same endpoint rather than running off the end
of the document.

## Prompts: audio with a spoken fallback

Every caller-audible moment is a pair — an audio URL and text. Audio wins;
otherwise Twilio speaks the text in one `TTS_VOICE`. One voice for the service,
not one per prompt: a show that speaks in two synthetic voices sounds broken
rather than varied.

Audio settings accept an absolute URL or a bare filename under `app/static`.
The filename form exists because `PUBLIC_BASE_URL` is a tunnel hostname that
rotates in development and `.env` cannot interpolate it, so a pasted absolute
URL goes stale on every restart. A filename is rebuilt against the current base
per request.

A filename that is not on disk counts as unset, so the prompt falls back to
its text rather than a `<Play>` of a 404, which Twilio skips and leaves the
caller in silence. Startup logs a warning naming any such file. Absolute URLs
are trusted as given: checking one would put a network request inside the
call webhook.

There are no bundled defaults. A greeting that fell back to a file on disk
used to mean clearing `GREETING_AUDIO_URL` kept playing the old recording.

The one asymmetry is **hold music**: it is music, so it has no text to fall
back on. Blank or missing plays one of Twilio's own classical tracks.

### How a hold times out

`<Enqueue waitUrl>` points at `/webhook/hold-wait`, not at the music. Twilio
requests it each time the previous track ends, with `QueueTime` attached, and
the answer is either one more track or, past `MAX_HOLD_MINUTES`, `<Leave>`.
Twilio then requests the `<Enqueue action>` with `QueueResult=leave`, and
`/webhook/queue-exit` plays the reject message and hangs up.

No timer of our own, deliberately. A timer dies with the process, and a
scaled-to-zero Cloud Run instance with the line left open and nobody watching
is exactly when a queue runs long. Twilio keeps asking regardless, and each
request wakes the instance. The cost is precision: the check runs between
tracks, so a hold can overrun by one track (about 2 minutes for the theme, 7-8
for Twilio's classical tracks).

Callers already holding when a new version deploys keep the waitUrl they were
enqueued with, so the limit applies from their next call.

### How big the queue can get

`HOLD_QUEUE_MAX_SIZE` is set on Twilio's queue at startup
(`services/hold_queue.py`), not in any TwiML. A size is a property of the
queue, so it applies to everyone. The app creates the queue itself if it does
not exist yet; leaving that to `<Enqueue>` would make it at Twilio's default
of 1000 until the next restart.

The size, not `MAX_HOLD_MINUTES`, is what bounds the cost of holding. If
callers ring straight back after timing out, the queue stays full, so the
bill is the queue size times the time the line is open.

### Turning calls away for free

`<Reject>` as the first verb is the one answer Twilio does not bill: the call
is never picked up. `/webhook/incoming-call` uses it, before any greeting, for
every caller the show cannot take:

- **Line closed** — busy signal. This used to be a spoken message, which cost
  a billed minute per call, so a flood between shows was the expensive case.
- **Queue full** — busy signal. Judged from the app's own count of open calls
  (still greeting, or holding), not from Twilio: asking would put a network
  request in the webhook, and Twilio's own `queue-full` only arrives after the
  greeting and speech recognition have been paid for. Callers mid-greeting
  count because each is about to join the queue.
- **Caller ID failed verification** (`StirVerstat` starting
  `TN-Validation-Failed`) — not-in-service. Spoofed numbers are how floods are
  placed. Twilio warns a failure can be its own certificate fetch, so
  `REJECT_FAILED_CALLER_ID` turns this off.
- **Blocked number** — not-in-service, and the contact's `blockedAttempts`
  goes up. Checked before the line and the queue, so a blocked number hears
  the same thing whatever the show is doing, and every attempt is counted.

None of them are registered: a call that was never answered is not in the
queue or history.

## Dependency direction

```
routes  ──▶  services  ──▶  schemas
   │            │
   └────────────┴──────▶  telephony
```

Routes depend on services; services never import routes. `main.py` is the only
place that knows about both — it builds the singletons and attaches them to
`app.state`, and `api/deps.py` is the only way routes reach them. That's what
lets each test build a fresh app with isolated state, which is why the suite
needs no cleanup fixtures.

`telephony/` is the containment boundary for Twilio: TwiML documents, signature
verification, REST control. Everything outside it deals only in
`app.schemas` types.

### On being Twilio-only

An earlier version had a provider abstraction with a Vonage renderer beside the
TwiML one. Only the response dialect was ever implemented — frame parsing,
signature verification and outbound audio all assumed Twilio — so
"multi-provider" was true of one file out of four and false everywhere it
mattered, and the structure let that claim pass unnoticed.

It's now honestly Twilio-only. The current design leans on `<Gather
input="speech">` and its `speechTimeout`, which have no direct equivalent
elsewhere, so a second provider is a real port rather than a config change.
If you do it, put signature verification and the TwiML/NCCO choice behind one
adapter object you cannot partially implement, so a half-supported provider
fails at startup instead of silently at 3am.

## Why there is no audio anywhere

The obvious design for live transcription is Twilio Media Streams: a websocket
delivering 20ms audio frames, forwarded to a streaming STT API. That's what
this repo did first (see commit `a124230`) and it needed frame parsing, a
bounded audio queue, backpressure handling, a keepalive loop, interim-vs-final
transcript reconciliation, and a mock STT mode to develop against.

All of it existed to answer one question: *has the caller stopped talking?*

`speechTimeout` answers that question inside Twilio, for free. The
transcript then arrives complete in a single webhook — no partial state, no
ordering to get right, no audio to buffer. Roughly 1,000 lines and two
dependencies deleted, for a system that does the same job.

The tradeoff is real and worth knowing: the operator sees nothing until the
caller finishes. If you ever need words appearing as they're spoken — to let an
operator cut in at second 4 of a 30-second ramble — that is the one thing this
design cannot do, and the streaming version is in git history.

## Concurrency model

One asyncio event loop, and nothing on a hot path anymore. Webhooks are
short-lived request handlers; the only long-lived connections are the dashboard
websockets, which run two tasks each (a reader and a writer, raced with
`asyncio.wait(FIRST_COMPLETED)` so a vanished client is noticed immediately
rather than at the next send).

The one piece of back-pressure machinery left is the broadcaster's per-client
bounded queue, which **drops oldest on overflow**. A dashboard that is behind
should jump to the present, not crawl through history.

## Failure behaviour

| Failure | Result |
| --- | --- |
| Caller says nothing | `actionOnEmptyResult` fires the webhook anyway; the call reaches the dashboard with an empty transcript rather than vanishing |
| `<Gather>` falls through | Trailing `<Redirect>` sends the call to the same endpoint instead of off the end of the document, which would hang up on the caller |
| Speech result for an unknown call | Revived into the queue if it was never decided (the caller was mid-greeting through a restart); logged and dropped if its saved row is decided or ended |
| Caller hangs up while on hold | Status callback ends the call and clears the queue card |
| Twilio retries the webhook | `register_incoming` is idempotent; no duplicate queue rows |
| Dashboard disconnects | Subscriber removed by the `subscribe()` context manager, even mid-send |
| Low transcription confidence | Surfaced in the UI rather than hidden — the operator is making a decision from that text |
| Database unusable at startup | Runs on one in memory; every dashboard shows a banner. See [Storage](#storage) |
| A save fails | Logged at ERROR and the call carries on; every dashboard shows the banner |

## Where a call lives

```
  ring ──▶ webhook ──▶ CallRegistry._calls ──save──▶ Store (SQLite)
                       (calls in play)                 │
                             │                         │
                        live queue                Call History
                        /api/calls                /api/call-history
                        WS snapshot               (paged, newest first)
```

Every change goes through `CallRegistry._commit`: save the row, broadcast,
and then, if the call is REJECTED or ENDED, drop it from memory. Saved first,
so nothing reaches a dashboard that a restart would take back. Memory holds
only calls in play — screening, holding, or on air until dial-complete ends
them — so nothing needs a cap.

`TERMINAL_STATUSES` in `schemas/calls.py` is the single definition of
finished. The queue filters on it (`is_open`) and the history query generates
its `IN` list from it, never a hand-written one. They drifted apart once, and
a resolved call stayed in the live queue as a result.

Store methods are synchronous like the registry's — see [Storage](#storage)
for why that is fine.

### Caller "location" is not a location

The label beside a caller ("Portland, OR") comes from Twilio's
`FromCity`/`FromState`/`FromCountry`, which it derives from the *number's*
rate centre. It describes where the number was issued, not where the person
is: a ported mobile keeps its original area code forever, and a VoIP number
can be registered anywhere. Treat it as a weak hint, never as fact, and do not
build routing or policy on it.

`format_location` picks the second component by country — state for NANP
(`Portland, OR`, since "US" tells a US operator nothing), country otherwise
(`London, GB`). It runs once in the webhook and the result is stored, so the
rule exists in Python only; the dashboard renders `caller.location` verbatim
rather than reimplementing it in TypeScript.

### The restart gap, and how it closes

A restart empties memory while Twilio still holds the callers.
`services/reconcile.py` asks Twilio who is queued and rebuilds the registry
from the answer. Twilio's Call resource has the number, the name and the start
time. It never had what the caller said, or `FromCity`/`FromState`/
`FromCountry` — those arrived as webhook parameters — so they come from the
row saved when the call rang. `Call.recovered` marks a rebuilt call whose
words were lost — no saved row, or one saved before their speech result — so
it does not look like a caller who said nothing. A row that has finished
meanwhile (they hung up while Twilio was being asked) is left alone.

Then the sweep. Once reconciliation has run, any saved call still open that it
did not restore ended while nobody was listening, and is closed as `ended`
with a NULL `ended_at`, which History shows as "ended during a restart". Only
after a reconcile that worked, including "no credentials" and "no queue": a
failed one sweeps nothing, because then nobody knows who is still holding.
That is why Twilio not answering raises `QueueUnknown` instead of looking like
an empty queue — the two mean opposite things here. With reconciliation off
(`RECONCILE_ON_STARTUP=false`, or demo mode), startup sweeps straight away.

Two more webhooks can reach a process that never saw the call. A speech result
from a caller who was mid-greeting through the restart revives them into the
queue, from the saved row or the webhook's own parameters. A dial result for a
caller on air through the restart closes the saved row with their on-air time.

Two implementation notes that are easy to get wrong:

- Reconciliation runs as a **background task**, not inside lifespan startup.
  Uvicorn does not accept connections until startup returns, so awaiting a
  slow Twilio there would make the number refuse new calls in order to recover
  old ones.
- `register_recovered` **refuses to overwrite a known call**. A caller can be
  mid-webhook while reconciliation runs, and a rebuilt row landing on top of a
  live one would throw away whatever arrived since.

### Two states, and what that costs

Closing the line does both halves at once: stops new callers *and* hangs up on
whoever is holding. Two states, open and closed, with no third.

There is an argument for three — off air but still working through the queue
you already have, which is arguably how a show actually ends. It was built that
way first and then collapsed, deliberately: two controls meant an operator
could leave the line closed with people still on it, which is the exact failure
everything else here exists to prevent. One button that always does the same
thing is harder to get wrong at the end of a long show.

The cost is real. You cannot go off air and keep taking the three good callers
you already had. If that becomes the thing people want, the split is cheap to
restore — `drain.py` never merged into `line_state.py`, and the endpoint calls
them in sequence rather than one implying the other.

### Every decision reaches the caller

`services/decisions.py` holds accept and reject. Both were logging stubs until
recently, and reject was the dangerous one: marking a call REJECTED takes it
out of `open_calls`, so it left the dashboard, while nothing reached Twilio and
the caller stayed in the hold queue listening to music. Stranded *and*
invisible — not in the queue, not cleared by closing the line (which only walks
open calls), still billed, still holding a slot against Twilio's 1000-call cap.

Accept had the same shape but fails loudly: nobody comes on air and you notice
in seconds. Reject looked completely fine on the dashboard, which is why it is
the one worth writing down.

Three rules they share with `drain.py`:

1. **The carrier acts first, local state follows.** A decision we could not
   deliver leaves the call where it was, so the dashboard is never more
   optimistic than the phone line.
2. **No credentials is an error, not a success.** The placeholder client used
   to log the command and return True, which made both decisions look like they
   worked anywhere. A screening dashboard whose whole job is showing what the
   caller is experiencing cannot afford that.
3. **Accept is strict about a 404, reject is lenient.** A vanished call means
   nobody is being put on air (failure), but it also means nobody is still
   holding (success). Same status code, opposite meanings.

### The two ends of the same problem

Twilio holds a caller forever and never tells us. Two features exist because of
that single fact, and they pull in opposite directions:

| | `services/reconcile.py` | `services/drain.py` |
| --- | --- | --- |
| Trigger | process start | an operator's click |
| Assumes | the callers should come back | the callers should be let go |
| On failure | empty queue, log line, no sweep | caller stays on the dashboard |

The temptation is to make draining a shutdown hook, which is why it is worth
writing down that it must not be. A deploy and a wrap-up are the same SIGTERM.
Draining on shutdown would hang up on live callers on every deploy and leave
reconciliation as code that only runs after a crash — the two features would
cancel each other out. Ending the show is a human decision, so it gets a
button, and shutdown stays recoverable.

Both order their side effects the same way: Twilio acts first, local state
follows. A caller we failed to hang up keeps their card on the dashboard rather
than vanishing from the queue while still connected, for the same reason
`register_recovered` refuses to overwrite a live call — the dashboard must
never be more optimistic than the carrier.

## Storage

`services/store.py` keeps call history and contacts in SQLite, through the
standard library's `sqlite3`: one file in a Docker volume, one connection, no
server. There was an earlier database, via SQLAlchemy, and it was removed for
costing an async driver, a session factory and a datetime type decorator to
serve one list. This one is plain SQL in one module.

Every `Store` method is synchronous, called straight from the async handlers
on the event-loop thread. Measured on this schema, a primary-key read takes
about 4µs and a caller's history about 20µs, so a cache, a thread hop or a lock
would buy nothing. `check_same_thread` stays on as a tripwire for anyone who
adds a thread.

**Migrations** are an append-only tuple in `services/migrations.py`, tracked by
`PRAGMA user_version`, each in its own transaction. Never edit a shipped one;
`test_migrations.py` freezes their hashes. A new column must be nullable or
have a default, and only SQLite 3.46 syntax is allowed — the Debian base in
`backend/Dockerfile` decides the containers' version, and a Mac's is newer.
`make test-container` runs the suite on the containers' SQLite.

The old database left one lesson. `create_all` never altered an existing
table, so dropping a model column left a `NOT NULL` orphan that failed every
insert — silently, because history writes were non-fatal, and invisibly to
tests, which built a fresh database every time. Now a test upgrades a database
populated at the previous version, and a failed write is logged at ERROR and
shown on every dashboard.

**Timestamps** are INTEGER milliseconds since the epoch, UTC, converted only by
`_to_ms` and `_from_ms`; a naive datetime is refused. The old database returned
naive datetimes, which shifted every time in the UI by the UTC offset.

**It degrades rather than refusing to start.** If the file cannot be used — a
volume the backend cannot write, a database left by newer code — `Store.open`
logs why and runs on a database in memory, and every dashboard shows a banner
that cannot be dismissed. The show crew updates unattended (`git pull`,
`make office`), so a backend that refused to start would take the phone line
down with it; one that forgets is still screening calls. The cost is spelled
out in the banner: an empty contacts table blocks nobody.

Transcripts are cleared after `TRANSCRIPT_RETENTION_DAYS` (default 90), at
startup and whenever the line closes; the calls themselves are kept. There is
no foreign key between calls and contacts: strangers have history, and
removing a contact keeps their calls. Withheld callers are saved with no
number, and a `CHECK` on the number columns is a tripwire so "anonymous" can
never be saved as one.

## Scaling out

Deliberately one uvicorn worker: `CallRegistry`, `Broadcaster` and `Store`
(one SQLite connection) are all in-process. When you outgrow it, in order:

1. **Move the fan-out to Redis pub/sub.** Keep `Broadcaster`'s interface; have
   `publish` write to a channel and each process subscribe and forward into its
   local queues.
2. **Move calls in play to Redis.** `CallRegistry` is already the only writer,
   so it's one class to reimplement.
3. **Move storage to a database server.** `Store` is the only code that speaks
   SQL, and a SQLite file serves one machine.

Note there is no sticky-routing requirement anymore. With no per-call
websocket to the provider, any worker can serve any webhook — which makes
horizontal scaling substantially easier than it was in the streaming design.

## Security posture

Currently suitable for a trusted network, not the public internet:

- The dashboard has no authentication and the API has no authorisation. Anyone
  who can reach `/ws/frontend` hears every caller's business.
- **Anyone who can reach the API can resolve any call.** Nothing stops an
  unauthenticated request from rejecting a caller you wanted on air. Put auth
  in front of `/api/calls` before this is reachable by anyone untrusted.
- Webhook signature validation exists but is off by default. Turn it on before
  pointing a real number at this — note it covers *every* webhook, because an
  unsigned post to `/webhook/speech-result` could put words in a caller's
  mouth, and one to `/webhook/call-status` could end a call while the caller
  still holds.
- Transcripts are personal data: a caller's number alongside whatever they
  chose to say about themselves. They are saved to disk, in the Docker volume,
  and cleared after `TRANSCRIPT_RETENTION_DAYS` (90 by default) with
  `secure_delete` on, so cleared text is overwritten rather than left in free
  pages. Backups from `make office-backup` keep them for good. They also reach
  every connected dashboard, and `LOG_LEVEL=DEBUG` writes their text to
  container logs — deliberately not the default.
