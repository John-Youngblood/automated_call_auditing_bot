# Running it on a machine in the office

One computer runs the screener. Everyone else opens it in a browser on the
office network. Twilio reaches it through a tunnel, so nobody touches the
router.

```
   Twilio ──HTTPS──▶ Cloudflare ──tunnel──▶ ┌─────────────────────────┐
                                            │   the office machine    │
                                            │                         │
   colleague's laptop ──LAN──▶ :5173 ───────│  nginx ──▶ backend      │
                                            └─────────────────────────┘
```

The tunnel points at the **backend**, not at the dashboard. Twilio needs the
webhooks and nothing else, so the dashboard stays on the LAN.

---

## Setting it up (once)

### 1. Install Docker Desktop

That is the only tool required. Everything else runs in containers, including
the tunnel.

### 2. Get the code and the config

```bash
git clone <this repo> && cd automated_call_auditing_bot
```

```bash
cp .env.example .env
```

### 3. Create the tunnel

Do this once, in Cloudflare Zero Trust → Networks → Tunnels → **Create a
tunnel** → *Cloudflared*. Give it a name, and when it offers install commands,
**copy the token** instead — the long string after `--token`.

Add a public hostname on the tunnel pointing at `http://backend:8000`. That
hostname is what Twilio will call, and it never changes.

> **Use a named tunnel, not `make tunnel`.** The quick tunnel is for
> development: it invents a new hostname on every restart, which means
> reconfiguring Twilio each time. A named tunnel's hostname is permanent, so
> Twilio is set up once and the office machine can reboot freely.

### 4. Fill in `.env`

```bash
APP_ENV=production
PUBLIC_BASE_URL=https://calls.yourdomain.com     # the tunnel's hostname
CLOUDFLARE_TUNNEL_TOKEN=eyJhIjoi…                # from step 3

HOST_PHONE_NUMBER=+15035551234                   # the host's phone
DASHBOARD_PASSWORD=                              # 8+ chars, shared with the room

TWILIO_ACCOUNT_SID=AC…
TWILIO_API_KEY_SID=SK…
TWILIO_API_KEY_SECRET=…
TWILIO_AUTH_TOKEN=…
VALIDATE_WEBHOOK_SIGNATURE=true
```

`APP_ENV=production` makes the service refuse to start on a missing dashboard
password or a placeholder host number, rather than discovering either mid-show.

### 5. Point Twilio at the tunnel hostname

Console → Phone Numbers → your number → Voice Configuration:

| Field | Value |
|---|---|
| A call comes in | `https://calls.yourdomain.com/webhook/incoming-call` |
| Primary handler fails | a TwiML Bin saying "try again shortly" |
| Call status changes | `https://calls.yourdomain.com/webhook/call-status` |

One-time, because the hostname is stable.

---

## Running it

On the office machine:

```bash
make office
```

or, the same thing spelled out:

```bash
docker compose -f docker-compose.office.yml up -d --build
```

> Note `up`, not `run`. `docker compose run` starts a one-off container for a
> single service and skips the others.

It prints both addresses. Colleagues use the LAN one:

```
  Dashboard on this machine: http://localhost:5173
  On the office network:     http://10.0.0.85:5173
```

They sign in with `DASHBOARD_PASSWORD`. Nothing to install on their machines.

```bash
make office-logs      # tail everything
```

```bash
make office-down      # stop
```

Containers are `restart: always`, so the stack comes back on its own after a
reboot or a Docker Desktop restart. You still need to be signed in to the OS
account Docker Desktop runs under.

---

## What this differs from `make up`

`make up` is the development stack: source bind-mounted, Vite dev server,
uvicorn `--reload`. Good for editing code, wrong for a show — a stray file save
restarts the backend and empties the queue.

The office stack builds the dashboard into static files behind nginx and bakes
the backend source into its image. No reloader, no bind-mounts, nothing that
restarts because someone opened an editor.

---

## The office machine has to stay awake

The screener is a single process holding the queue in memory. If the machine
sleeps, calls stop being answered.

- **macOS**: System Settings → Lock Screen → *Turn display off…* is fine, but
  set Energy → **Prevent automatic sleeping when the display is off**.
- **Windows**: Settings → Power → Sleep → **Never** on mains power.

A reboot is survivable — the containers restart and the backend asks Twilio who
is still holding — but sleep mid-show is not, because nothing is listening.

---

## When something is wrong

**Nobody can reach the dashboard.** Check the machine's LAN address has not
changed (DHCP). A static lease on the router is worth the five minutes.

**Calls reach Twilio but not us.** `make office-logs` and look for the
`cloudflared` lines. If the tunnel is down, Twilio plays the caller your
fallback TwiML Bin, which is the whole reason to have set one.

**"Sign in to the dashboard first" right after signing in.** The backend
restarted and forgot every session — tokens are in memory. Sign in again.

**The queue is empty but people are calling.** Check the line is open (the
footer says `LINE OPEN`). `LINE_OPEN_ON_START=true` means a restart comes back
open, so this usually means someone closed it.

---

## What it does not give you

- **No HTTPS on the LAN.** Colleagues reach `http://10.0.0.85:5173` in the
  clear. Fine on a trusted office network, not fine over shared Wi-Fi with
  guests on it. The password goes over that connection.
- **The dashboard is only as available as the machine.** No failover.
- **State still dies with the process.** A restart is recoverable because the
  queue is rebuilt from Twilio, but transcripts are not. See the main README.
