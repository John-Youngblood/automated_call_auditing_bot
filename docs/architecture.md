# Architecture notes

Companion to the README: why the structure is what it is, and what to change
when the assumptions stop holding.

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
