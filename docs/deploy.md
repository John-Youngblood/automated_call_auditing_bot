# Deploying to Google Cloud

One container on Cloud Run, serving both the API and the dashboard:

```
  Twilio ──webhooks──▶  ┌──────────────────────────┐  ◀──dashboard──  you
                        │  Cloud Run, one instance │
                        │  FastAPI + built React   │
                        └──────────────────────────┘
```

The dashboard is baked into the image by the repo-root `Dockerfile` and served
at `/`, so there is one origin, one URL and one deploy. That is what keeps CORS
and the build-time `VITE_*` URLs out of this document entirely — unset, the
client derives both from `window.location`.

The backend is **one container pinned to at most one instance**. Call state,
the dashboard fan-out and the line-open flag all live in memory, so a second
instance is a second queue that nobody is watching. `--max-instances=1` is not
a cost control here, it is a correctness requirement.

`/api/*` and the dashboard websocket are behind `DASHBOARD_PASSWORD`.
`/webhook/*` is not, and must not be — Twilio carries no credentials of ours,
and those requests are authenticated by signature instead.

---

## Prerequisites

```bash
gcloud auth login
```

```bash
gcloud config set project YOUR_PROJECT_ID
```

```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com
```

No Node and no Docker needed locally — Cloud Build builds the image, including
the dashboard.

---

## 1. Secrets

Credentials do **not** belong in `--set-env-vars`: those are readable by anyone
with `run.services.get` and land in your shell history.

```bash
for s in dashboard-password twilio-auth-token twilio-account-sid twilio-api-key-sid twilio-api-key-secret; do
  gcloud secrets create "$s" --replication-policy=automatic
done
```

Then put each value in, reading from a prompt rather than an argument:

```bash
read -rs SECRET && printf '%s' "$SECRET" | gcloud secrets versions add twilio-auth-token --data-file=-
```

Repeat for the others. The service only insists on 8 characters, but this
deployment puts the dashboard on a public URL with no rate limiting, and nobody
has to memorise a secret they paste in once — so generate it:

```bash
openssl rand -base64 24 | tr -d '\n' | gcloud secrets versions add dashboard-password --data-file=-
```

Grant the runtime service account read access:

```bash
PROJECT_NUMBER=$(gcloud projects describe "$(gcloud config get-value project)" --format='value(projectNumber)')
for s in dashboard-password twilio-auth-token twilio-account-sid twilio-api-key-sid twilio-api-key-secret; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor
done
```

---

## 2. Deploy

Deploy once to get the URL. `PUBLIC_BASE_URL` has to *be* that URL — every
TwiML callback is built from it — so the first deploy is deliberately
incomplete and the second one fills it in.

```bash
gcloud run deploy call-screener \
  --source=. \
  --region=us-west1 \
  --allow-unauthenticated \
  --min-instances=0 --max-instances=1 \
  --cpu=1 --memory=512Mi \
  --concurrency=80 \
  --no-cpu-throttling \
  --timeout=3600 \
  --set-env-vars="APP_ENV=production,HOST_PHONE_NUMBER=+15035551234" \
  --set-secrets="DASHBOARD_PASSWORD=dashboard-password:latest,TWILIO_AUTH_TOKEN=twilio-auth-token:latest,TWILIO_ACCOUNT_SID=twilio-account-sid:latest,TWILIO_API_KEY_SID=twilio-api-key-sid:latest,TWILIO_API_KEY_SECRET=twilio-api-key-secret:latest"
```

`--source=.` is the repo root on purpose: the root `Dockerfile` builds the
dashboard in a Node stage and copies it into the Python image. `.gcloudignore`
keeps `node_modules`, `.venv` and `.env` out of the upload.

### What every flag is doing

| Flag | Why |
|---|---|
| `--source=.` | Root `Dockerfile` builds dashboard + API into one image. `--source=backend` would ship the API alone. |
| `--max-instances=1` | Two instances are two different in-memory queues, and the caller you can see is in the other one. **Correctness, not cost.** |
| `--min-instances=0` | See below — the one knob that changes between a demo and a show. |
| `--cpu=1 --memory=512Mi` | The floor that comfortably runs Python + FastAPI. Memory holds the queue and recent history, both small. |
| `--concurrency=80` | One instance serves every caller webhook *and* every open dashboard socket. The default of 80 is ample; lowering it would make Cloud Run want a second instance, which `--max-instances=1` forbids, so requests would queue instead. |
| `--no-cpu-throttling` | CPU outside a request is throttled by default. Startup reconciliation is a background task, and the dashboard websocket pushes between requests — both need CPU when no request is in flight. |
| `--timeout=3600` | The dashboard websocket is one long-lived request. The 5-minute default would drop every dashboard every 5 minutes. |
| `--allow-unauthenticated` | Twilio's webhooks carry no Google credentials. `DASHBOARD_PASSWORD` guards the human surface instead. |
| `--region` | Put it near the host and the Twilio number. Region is fixed at create time; changing it means a new service and a new URL. |

### `--min-instances`: 0 for a demo, 1 for a show

This is the only flag worth revisiting, and it is the whole bill.

**`--min-instances=0`** — nothing runs when nobody is using it, so idle cost is
essentially zero. Good for a demo you show occasionally. Two consequences:

* The first call after an idle period pays a cold start while the container
  boots. Twilio allows 15s for a webhook response, so this usually lands, but
  it is a race you would not want on air.
* Nothing survives between sessions. That matters less than it sounds, because
  an open dashboard holds a websocket, and that request keeps the instance
  alive for as long as anyone is watching.

**`--min-instances=1`** — one instance always resident. No cold starts, and the
queue persists between calls. This bills continuously whether or not the phone
rings: you are renting a small always-on container, not paying per call. Use it
for a real episode:

```bash
gcloud run services update call-screener --region=us-west1 --min-instances=1
```

and put it back to `0` afterwards. Check the current rate on the
[pricing calculator](https://cloud.google.com/products/calculator) — it is the
only meaningful line on the bill.

### Then fill in the URL

Take the URL the deploy printed and redeploy with it:

```bash
gcloud run services update call-screener --region=us-west1 \
  --update-env-vars="PUBLIC_BASE_URL=https://call-screener-xxxx.run.app,VALIDATE_WEBHOOK_SIGNATURE=true"
```

`VALIDATE_WEBHOOK_SIGNATURE` only works once `PUBLIC_BASE_URL` is right — the
signature covers the URL Twilio requested, and this service rebuilds it from
that setting rather than trusting forwarded headers.

Open the `.run.app` URL and sign in with the dashboard password. If the header
says **Connected**, the websocket is through and you are done.

---

## 3. Point Twilio at it

Console → Phone Numbers → your number → Voice Configuration:

| Field | Value |
|---|---|
| A call comes in | `https://call-screener-xxxx.run.app/webhook/incoming-call` |
| Primary handler fails | a TwiML Bin saying "try again shortly" |
| Call status changes | `https://call-screener-xxxx.run.app/webhook/call-status` |

The `.run.app` hostname is stable across deploys, so unlike the dev tunnel this
is a one-time step.

---

## Redeploying

```bash
gcloud run deploy call-screener --source=. --region=us-west1
```

Flags set earlier are remembered; only the image changes. Because the dashboard
now lives in the same image, a frontend-only change is a backend deploy too.

A new revision starts a fresh container with an empty queue while the old one
drains. That is survivable **because of startup reconciliation**: the new
instance asks Twilio who is still in the hold queue and rebuilds the dashboard
from the answer.

What it cannot rebuild is transcripts — they only ever existed in memory. Those
callers come back flagged as recovered, with no stated reason. Fine at 2am,
worth avoiding at 9pm.

---

## Locking it down

What is in place:

* `DASHBOARD_PASSWORD` guards `/api/*` and the dashboard websocket.
* `VALIDATE_WEBHOOK_SIGNATURE=true` authenticates `/webhook/*` per request,
  which is stronger than a shared secret because Twilio signs each one.

What is not, in rough order of what it buys:

1. **Per-user identity.** One password means no record of who rejected a
   caller, and rotating it signs everyone out at once.
2. **Rate limiting on the login.** Nothing here slows a guessing loop down, so
   the password's own length is all that stands between a public URL and one.
   The 8-character floor is a sanity check, not a defence — hence generating
   the password above. Cloud Armor in front of the service would fix the gap
   properly.
3. **Identity-Aware Proxy** on the dashboard, if you would rather use Google
   accounts than a shared secret. Note it cannot cover `/webhook/*`, so the
   service would still need to be reachable unauthenticated on those paths.

---

## If you would rather not run it in the cloud

Most people using this will run it on a machine in their own office, which
needs no Google account and no per-month cost. See [office.md](office.md).
