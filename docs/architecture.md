# Architecture notes

Companion to the README: the reasoning behind the structure, and what to change
when the assumptions stop holding.

## Dependency direction

```
routes  ──▶  services  ──▶  schemas
   │            │
   └────────────┴──────▶  telephony
```

Routes depend on services; services never import routes. `main.py` is the only
place that knows about both — it builds the singletons and attaches them to
`app.state`, and `api/deps.py` is the only way routes reach them. That is what
lets a test build a fresh app per test with isolated state, which is why the
suite has no cross-test leakage and needs no cleanup fixtures.

`telephony/` is the containment boundary for provider quirks: payload shapes,
XML vs JSON dialects, frame framing, signature schemes. Supporting a new
provider means adding a module there, not editing routes or services.

## Concurrency model

One asyncio event loop. Per call there are three tasks:

| Task | Lives in | Blocking rule |
| --- | --- | --- |
| media receive loop | `routes/audio_stream.py` | Never awaits anything slower than the socket |
| STT pump | `services/screening.py` | Owns the only `await` to Deepgram |
| Deepgram reader | `services/deepgram.py` | Publishes transcripts, never back to the pump |

Plus two per dashboard (a reader and a writer, raced with
`asyncio.wait(FIRST_COMPLETED)` so a vanished client is noticed immediately
rather than at the next send).

Two bounded queues decouple the stages, and **both drop oldest on overflow**:

- `screening.AUDIO_QUEUE_MAX` (100 frames ≈ 2s) — between the socket and STT.
- `broadcaster` queue (`FRONTEND_QUEUE_MAX`, default 250) — per dashboard.

Dropping the oldest is the right policy for both. This is live audio: a
five-second-old frame has no value, and a dashboard that is behind should jump
to the present rather than crawl through history.

## Failure behaviour

| Failure | Result |
| --- | --- |
| Deepgram unreachable at call start | Call proceeds untranscribed; `stream.status: degraded` reaches the dashboard |
| Deepgram drops mid-call | Session marks itself degraded; `send_audio` becomes a no-op; call continues |
| Malformed provider frame | Logged and skipped; one bad frame out of ~50/sec never ends a call |
| Dashboard disconnects | Subscriber removed by the `subscribe()` context manager, even mid-send |
| Media stream closes | `finally` flushes STT and ends the call — teardown runs on every exit path |
| Provider retries the webhook | `register_incoming` is idempotent; no duplicate queue rows |

## Moderation data flow

```
  ring ──▶ webhook ──▶ BlocklistService.is_blocked()   [in-memory set, no I/O]
                            │
                 blocked ───┼─── not blocked
                            │            │
                   <Reject> │            ▼
                   history  │       queue + media stream + STT
                            ▼
                      (no stream, no STT, no billing)
```

The blocklist is read on the webhook path while the provider holds the caller
waiting, so it is a cached set rather than a query. The cache is loaded once at
startup and updated on every write, so it cannot drift *within* a process — the
same single-worker constraint the broadcaster and registry already impose.

`call_history` is written on terminal transitions only, which is why
`CallRegistry.set_status` and `.end` are async while the rest of the class is
sync: they fire once per call, well off the audio hot path.

### Two failure modes worth knowing

**Normalisation is the whole feature.** A blocklist that stores what the
moderator typed and compares it against what the carrier sends will silently
never match. Everything goes through `services/phone.py` on the way in and on
the way to a comparison. If you need real carrier-grade parsing (extensions,
short codes, arbitrary national formats), swap that module's body for
`phonenumbers`; nothing else depends on how it works.

**SQLite has no timezone-aware datetime type.** `DateTime(timezone=True)` is a
no-op there: aware values go in, naive ones come back, Pydantic serialises them
with no offset, and the browser reads them as local time — shifting every
entry in the moderation log by the UTC offset. The `UtcDateTime` type decorator
in `db/models.py` normalises both directions; use it for any datetime column
you add.

## Scaling out

The current limit is deliberate: `CallRegistry` and `Broadcaster` are
in-process, so the deployment runs a single uvicorn worker. One process handles
a lot of concurrent calls — the work per call is I/O, not CPU — but it is a
single point of failure and it caps you at one machine.

When you outgrow it, in order:

1. **Move the fan-out to Redis pub/sub.** Keep `Broadcaster`'s interface; have
   `publish` write to a channel and each process subscribe and forward into its
   local queues. The bounded-queue policy stays exactly as it is. Publish
   block events on the same channel so the blocklist caches stay in step —
   otherwise a number blocked on worker A keeps getting through on worker B.
2. **Move call state to Redis or Postgres.** `CallRegistry` is already the only
   writer, so this is one class to reimplement. Finished calls are already
   durable in `call_history`; it is the *in-flight* calls that are
   process-local. Swap SQLite for Postgres at the same time — the models are
   plain SQLAlchemy, but `create_all` is not a migration story, so add Alembic
   before the history table holds anything you would miss.
3. **Route media streams by call id.** Audio websockets are sticky to the
   process holding the call's STT connection; a consistent hash on `callId` at
   the load balancer is the usual answer.

Note that steps 1 and 2 also unlock horizontal restarts without dropping
in-flight calls, which matters more than raw throughput for most deployments.

## The interim/final transcript contract

Deepgram emits interim hypotheses before committing. The registry keeps one
`segment_id` open per utterance: interim results reuse it, a final result closes
it, and the next interim starts a new one. The dashboard replaces a line whose
`segmentId` it already has, and appends otherwise.

This is the thing to preserve if you swap STT providers — get it wrong in one
direction and text duplicates on screen, wrong in the other and it overwrites
finished sentences.

## Security posture

Currently suitable for a trusted network, not the public internet:

- The dashboard has no authentication and the API has no authorisation. Anyone
  who can reach `/ws/frontend` hears every caller's transcript.
- Webhook signature validation exists but is off by default
  (`VALIDATE_WEBHOOK_SIGNATURE`). Turn it on before pointing a real number at
  this.
- `/ws/audio-stream` cannot be authenticated — providers send no credentials.
  Identify the call from the `callId` parameter planted in your own webhook
  response, and restrict ingress to the provider's published IP ranges.
- Call audio and transcripts are personal data, and in a healthcare context very
  likely PHI. Nothing here is encrypted at rest, retained deliberately, or
  deleted on a schedule — decide those before this handles a real call. The
  `call_history` table makes this sharper, not looser: transcripts now persist
  past process restart, so retention is a decision you are already making by
  default.
- **Anyone who can reach the API can block any number.** There is no
  authentication, so `blockedBy` is self-reported and worthless for audit, and
  nothing stops a bad actor from blocking your most important callers. Put auth
  in front of `/api/block-number` before this is reachable by anyone untrusted.
- There is no unblock path. That is deliberate for now (the confirmation copy
  says as much), but it means a mistaken block needs a database edit.
