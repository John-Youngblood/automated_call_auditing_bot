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
input="speech">` and `speechTimeout="auto"`, which have no direct equivalent
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

`speechTimeout="auto"` answers that question inside Twilio, for free. The
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

## Moderation data flow

```
  ring ──▶ webhook ──▶ BlocklistService.is_blocked()   [in-memory set, no I/O]
                            │
                 blocked ───┼─── not blocked
                            │            │
                  <Reject>  │            ▼
                  history   │      <Gather> → transcript → queue
                            ▼
                  (never answered, never billed)
```

The blocklist is read while Twilio holds the caller waiting, so it's a cached
set rather than a query. Loaded once at startup and updated on every write, so
it cannot drift *within* a process — the same single-worker constraint the
broadcaster and registry impose.

`call_history` is written on terminal transitions only, which is why
`CallRegistry.set_status` and `.end` are async while the rest of the class is
sync: they fire once per call.

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

### Two failure modes worth knowing

**Normalisation is the whole blocklist feature.** A blocklist that stores what
the moderator typed and compares it against what the carrier sends will
silently never match. Everything goes through `services/phone.py` on the way in
and on the way to a comparison. If you need real carrier-grade parsing
(extensions, short codes, arbitrary national formats), swap that module's body
for `phonenumbers`; nothing else depends on how it works.

**`create_all` is not a migration.** It creates missing tables and never
alters existing ones. Dropping `transcript_line_count` from the model left the
column in the database, still `NOT NULL` -- so every history insert failed the
constraint. Because `CallHistoryRepository.record` deliberately swallows its
errors (history is not worth failing a call teardown over), the app looked
healthy while losing every row. Tests did not catch it: they build a fresh
in-memory database per test, so the table always matches the models.

`Database.create_schema` now diffs live tables against the models at boot and
raises `SchemaDriftError` on a leftover non-nullable column, naming the column
and the fix. That converts silent data loss into a refusal to start.

**SQLite has no timezone-aware datetime type.** `DateTime(timezone=True)` is a
no-op there: aware values go in, naive ones come back, Pydantic serialises them
with no offset, and the browser reads them as local time — shifting every entry
in the moderation log by the UTC offset. The `UtcDateTime` type decorator in
`db/models.py` normalises both directions; use it for any datetime column you
add.

## Contacts

`contacts` holds the team's own labels on a number: a display name, a star, or
both. One table rather than two, because naming and starring are the same act.

Kept separate from `blocked_numbers`, though, which answers a different
question and carries its own provenance (who blocked, when, why). A number can
appear in both — naming a nuisance caller is precisely how you recognise them.

Resolution happens at two different moments, on purpose:

- **Live calls** resolve name and star in the webhook, once, as the call
  arrives. Cheap, and the dashboard renders without cross-referencing.
  Naming someone mid-call therefore leaves the call stale, so the contacts
  route re-resolves calls already in flight and republishes them.
- **History rows** resolve at read time against the current contact, so naming
  a caller retroactively labels every past call from them.

A contact that ends up with no name and no star is deleted rather than stored,
which keeps "remove name" safe to offer whether or not a saved name existed.

## Scaling out

Deliberately one uvicorn worker: `CallRegistry`, `Broadcaster` and the
blocklist cache are all in-process. When you outgrow it, in order:

1. **Move the fan-out to Redis pub/sub.** Keep `Broadcaster`'s interface; have
   `publish` write to a channel and each process subscribe and forward into its
   local queues. Publish block events on the same channel so the blocklist
   caches stay in step — otherwise a number blocked on worker A keeps getting
   through on worker B.
2. **Move in-flight call state to Redis or Postgres.** `CallRegistry` is
   already the only writer, so it's one class to reimplement. Finished calls
   are already durable. Swap SQLite for Postgres at the same time — the models
   are plain SQLAlchemy, but `create_all` is not a migration story, so add
   Alembic before `call_history` holds anything you'd miss.

Note there is no sticky-routing requirement anymore. With no per-call
websocket to the provider, any worker can serve any webhook — which makes
horizontal scaling substantially easier than it was in the streaming design.

## Security posture

Currently suitable for a trusted network, not the public internet:

- The dashboard has no authentication and the API has no authorisation. Anyone
  who can reach `/ws/frontend` hears every caller's business.
- **Anyone who can reach the API can block any number.** `blockedBy` is
  self-reported and worthless for audit, and nothing stops someone blocking
  your most important callers. Put auth in front of `/api/block-number` before
  this is reachable by anyone untrusted.
- Webhook signature validation exists but is off by default. Turn it on before
  pointing a real number at this — note it covers *both* webhooks, because an
  unsigned post to `/webhook/speech-result` could put words in a caller's
  mouth.
- Transcripts are personal data, and in a healthcare context likely PHI.
  `call_history` persists them past restart, so retention is a decision you are
  already making by default. Nothing here is encrypted at rest or deleted on a
  schedule.
- There is no unblock path. Deliberate for now (the confirmation copy says as
  much), but a mistaken block needs a database edit.
