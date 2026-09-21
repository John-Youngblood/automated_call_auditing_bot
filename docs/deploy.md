# Deploying to Google Cloud

Two pieces, two places:

```
  Twilio ──webhooks──▶  Cloud Run (backend)  ◀──API + wss──  Firebase Hosting (dashboard)
                        one instance, pinned                 static bundle, CDN
```

The backend is **one container, pinned to exactly one instance**. Call state,
the dashboard fan-out and the on-air flag all live in memory, so a second
instance is a second queue that nobody is watching. `--max-instances=1` is not
a cost control here, it is a correctness requirement.

The dashboard and the whole `/api` surface are behind `DASHBOARD_PASSWORD`.
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
gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
```

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

Repeat for the others. For the dashboard password, generate one rather than
choosing it — the service rejects anything under 16 characters, and it is a
shared secret on a public URL with no rate limiting:

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

## 2. Backend → Cloud Run

Deploy once to get the URL. `PUBLIC_BASE_URL` has to *be* that URL — every
TwiML callback is built from it — so the first deploy is deliberately
incomplete, and the second one fills it in.

```bash
gcloud run deploy call-screener \
  --source=backend \
  --region=us-west1 \
  --allow-unauthenticated \
  --min-instances=1 --max-instances=1 \
  --no-cpu-throttling \
  --timeout=3600 \
  --set-env-vars="APP_ENV=production,HOST_PHONE_NUMBER=+15035551234" \
  --set-secrets="DASHBOARD_PASSWORD=dashboard-password:latest,TWILIO_AUTH_TOKEN=twilio-auth-token:latest,TWILIO_ACCOUNT_SID=twilio-account-sid:latest,TWILIO_API_KEY_SID=twilio-api-key-sid:latest,TWILIO_API_KEY_SECRET=twilio-api-key-secret:latest"
```

Every flag on that command is load-bearing:

| Flag | Why |
|---|---|
| `--min-instances=1` | Scale-to-zero means a cold start on the next call, and Twilio times out. It also means losing the queue. |
| `--max-instances=1` | Two instances are two different queues. **Correctness, not cost.** |
| `--no-cpu-throttling` | Startup reconciliation runs as a background task, and CPU outside a request is throttled by default. |
| `--timeout=3600` | The dashboard websocket is one long request. The 5-minute default would drop it every 5 minutes. |
| `--allow-unauthenticated` | Twilio's webhooks carry no Google credentials. `DASHBOARD_PASSWORD` guards the human surface instead. |

Take the URL it prints, then redeploy with it:

```bash
gcloud run services update call-screener --region=us-west1 \
  --update-env-vars="PUBLIC_BASE_URL=https://call-screener-xxxx.run.app,VALIDATE_WEBHOOK_SIGNATURE=true"
```

`VALIDATE_WEBHOOK_SIGNATURE` only works once `PUBLIC_BASE_URL` is right — the
signature covers the URL Twilio requested, and this service rebuilds it from
that setting rather than trusting forwarded headers.

---

## 3. Dashboard → Firebase Hosting

`VITE_*` values are **baked in at build time**, so the backend URL must exist
first. That is the whole reason for the ordering.

```bash
cd frontend
```

```bash
VITE_API_BASE_URL=https://call-screener-xxxx.run.app \
VITE_WS_URL=wss://call-screener-xxxx.run.app/ws/frontend \
npm run build
```

Note `wss://`, not `https://`. With the two on one origin the client derives
the scheme from `window.location`; split apart, it has to be told.

```bash
firebase deploy --only hosting
```

`firebase.json` is already here: it serves `dist`, rewrites everything to
`index.html` so a deep link doesn't 404, and caches the fingerprinted assets
hard while keeping `index.html` uncached.

---

## 4. Let the two origins talk

They are now on different hosts, so the browser enforces CORS. Tell the backend
which origin to trust:

```bash
gcloud run services update call-screener --region=us-west1 \
  --update-env-vars="CORS_ALLOW_ORIGINS=https://YOUR_SITE.web.app"
```

Exact origin, no trailing slash, and not `*` — credentials are allowed on these
requests, and a wildcard would let any page on the internet drive your line.

---

## 5. Point Twilio at it

Console → Phone Numbers → your number → Voice Configuration:

| Field | Value |
|---|---|
| A call comes in | `https://call-screener-xxxx.run.app/webhook/incoming-call` |
| Primary handler fails | a TwiML Bin saying "try again shortly" |
| Call status changes | `https://call-screener-xxxx.run.app/webhook/call-status` |

The `.run.app` hostname is stable across deploys, so unlike the dev tunnel this
is a one-time step.

---

## Deploys during a show

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
2. **Rate limiting on the login.** The 16-character floor is the only thing
   standing between a public URL and an offline-speed guessing loop. Cloud
   Armor in front of the service would fix it.
3. **Identity-Aware Proxy** on the dashboard, if you would rather use Google
   accounts than a shared secret. Note it cannot cover `/webhook/*`, so the
   service would still need to be reachable unauthenticated on those paths.

---

## Costs worth knowing

`--min-instances=1` with `--no-cpu-throttling` bills continuously rather than
per request — you are renting a small always-on container, not paying per call.
That is unavoidable given the in-memory state; scale-to-zero would drop the
queue. Firebase Hosting for a bundle this size sits in the free tier.
