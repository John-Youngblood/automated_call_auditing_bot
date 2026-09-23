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
    routes/frontend.py   WS /ws/frontend
  services/
    call_registry.py     single writer of call state, live + recent
    broadcaster.py       bounded-queue fan-out to dashboards
    decisions.py         accept and reject, carrier-first
    drain.py             hang up on holders when the line closes
    line_state.py        on air / off air, broadcast to dashboards
    reconcile.py         rebuild the queue from Twilio on boot
    phone.py             caller location labels
  telephony/             everything Twilio-specific
    twiml.py             the five XML documents
    rest.py              the four REST endpoints we call
    signature.py         webhook authenticity
  static/                audio files the settings name, served at /static

frontend/src/
  hooks/useCallStream.ts one socket, one reducer
  hooks/useCallHistory.ts history is a plain HTTP read
  components/            presentational only
  types/events.ts        mirrors backend/app/schemas/
```

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
back on. Blank or missing omits `waitUrl`, and Twilio plays its own playlist.

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
| Speech result for an unknown call | Logged and dropped; Twilio retries can outlive a call |
| Caller hangs up while on hold | Status callback ends the call and clears the queue card |
| Twilio retries the webhook | `register_incoming` is idempotent; no duplicate queue rows |
| Dashboard disconnects | Subscriber removed by the `subscribe()` context manager, even mid-send |
| Low transcription confidence | Surfaced in the UI rather than hidden — the operator is making a decision from that text |

## Where a call lives

```
  ring ──▶ webhook ──▶ CallRegistry._calls   ──▶  is_open?
                       (one dict, in memory)       │
                                         yes ──────┼────── no
                                          │                │
                                  live queue          Call History
                                  /api/calls          /api/call-history
                                  WS snapshot         (newest first, capped)
```

One dict holds both. A call does not move or get copied when it finishes — it
simply stops being `is_open`, and `TERMINAL_STATUSES` in `schemas/calls.py` is
the single definition of which statuses mean that. The queue filter and the
history view reading the same predicate is deliberate: they drifted apart once,
and a resolved call stayed in the live queue as a result.

`_prune_terminal` drops the oldest finished calls past `CALL_HISTORY_SIZE`.
Nothing else evicts, so that cap is simultaneously the retention policy and the
only thing bounding memory.

Because nothing is persisted, every registry method is synchronous. They used
to be async to await a SQLite write; with the write gone, so is the reason.

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

### The restart gap, and how far it closes

Dropping the database bought simplicity and cost one thing: a restart loses
every in-flight call while Twilio still holds the callers. `services/reconcile.py`
closes most of that gap by asking Twilio who is queued and rebuilding the
registry from the answer.

What it cannot close is the transcript. Twilio's Call resource has the number,
the name and the start time; it has never had what the caller said, because
that reached us as a `SpeechResult` webhook parameter and nowhere else. The
same is true of `FromCity`/`FromState`/`FromCountry`.

That asymmetry is the whole design constraint. Everything cheap to recover is
recovered; the one expensive thing is marked (`Call.recovered`) so the UI can
be honest about it rather than rendering a recovered caller identically to one
who stayed silent. If that ever stops being good enough, the minimal fix is
not "add a database back" — it is persisting *only* the transcript, keyed by
CallSid, which is the sole field Twilio cannot give you.

Two implementation notes that are easy to get wrong:

- Reconciliation runs as a **background task**, not inside lifespan startup.
  Uvicorn does not accept connections until startup returns, so awaiting a
  slow Twilio there would make the number refuse new calls in order to recover
  old ones.
- `register_recovered` **refuses to overwrite a known call**. A caller can be
  mid-webhook while reconciliation runs, and a blank recovered row landing on
  top of a live one would destroy the transcript that survived.

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
| On failure | empty queue, log line | caller stays on the dashboard |

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

### Why there is no database

There was one: SQLite via SQLAlchemy, holding a blocklist and a `call_history`
table. Both are gone, and the lesson is worth keeping.

The history table's only reader was a list of recent calls on one dashboard.
For that, it cost an async driver, a session factory, a `UtcDateTime` type
decorator (SQLite returns naive datetimes, which silently shifted every
timestamp in the UI by the UTC offset), a schema-drift check at boot, and a
volume in compose. It also produced the nastiest bug in this project's history:
`create_all` never alters an existing table, so dropping a model column left a
`NOT NULL` orphan that failed every insert — silently, because history writes
were deliberately non-fatal, and invisibly to tests, which built a fresh
database per test where the schema always matched.

Holding those calls in the registry instead costs one `sorted()` and a cap.
The trade is that history resets on restart. That is a real loss and an
acceptable one here; if it stops being acceptable, add storage back for that
one feature deliberately, rather than because a scaffold arrived with it.

## Scaling out

Deliberately one uvicorn worker: `CallRegistry` and `Broadcaster` are both
in-process. When you outgrow it, in order:

1. **Move the fan-out to Redis pub/sub.** Keep `Broadcaster`'s interface; have
   `publish` write to a channel and each process subscribe and forward into its
   local queues.
2. **Move call state to Redis.** `CallRegistry` is already the only writer, so
   it's one class to reimplement, and doing so makes history shared across
   workers and survive a restart in the same move.

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
  pointing a real number at this — note it covers *both* webhooks, because an
  unsigned post to `/webhook/speech-result` could put words in a caller's
  mouth.
- Transcripts are personal data: a caller's number alongside whatever they
  chose to say about themselves. Nothing is written to disk, which limits the
  exposure, but they do sit in memory for the last `CALL_HISTORY_SIZE` calls
  and they reach every connected dashboard. `LOG_LEVEL=DEBUG` also writes their
  text to container logs — deliberately not the default.
