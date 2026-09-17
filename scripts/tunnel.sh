#!/usr/bin/env bash
# Open a public tunnel to the local backend and point the app at it.
#
# A quick Cloudflare tunnel gets a new random hostname every run, and that
# hostname has to reach three places or calls fail in confusing ways:
#
#   1. PUBLIC_BASE_URL in .env  -- the TwiML builds absolute URLs from it
#   2. the backend container    -- compose only reads .env when it *creates*
#                                  a container, so a restart is not enough
#   3. the Twilio console       -- the number's voice webhook
#
# This script does 1 and 2, and prints what to paste for 3.
#
#   ./scripts/tunnel.sh          # tunnel to localhost:8000
#   ./scripts/tunnel.sh 9000     # ...or another port
#
# Leave it running. Ctrl-C closes the tunnel.
set -euo pipefail

PORT="${1:-8000}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL_FILE="$ROOT/.tunnel-url"
LOG="$(mktemp -t call-screener-tunnel)"

command -v cloudflared >/dev/null || {
  echo "cloudflared is not installed. brew install cloudflared" >&2
  exit 1
}

curl -fsS -o /dev/null "http://localhost:$PORT/healthz" || {
  echo "Nothing healthy on localhost:$PORT -- start the stack first:" >&2
  echo "  docker compose up -d" >&2
  exit 1
}

echo "Opening a tunnel to localhost:$PORT ..."
cloudflared tunnel --url "http://localhost:$PORT" --no-autoupdate >"$LOG" 2>&1 &
TUNNEL_PID=$!
trap 'kill "$TUNNEL_PID" 2>/dev/null || true; rm -f "$URL_FILE"' EXIT

URL=""
for _ in $(seq 1 30); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)
  [ -n "$URL" ] && break
  kill -0 "$TUNNEL_PID" 2>/dev/null || { echo "cloudflared exited:" >&2; cat "$LOG" >&2; exit 1; }
  sleep 2
done
[ -n "$URL" ] || { echo "Timed out waiting for a tunnel URL:" >&2; tail -20 "$LOG" >&2; exit 1; }

echo "$URL" > "$URL_FILE"

# 1. Point the app at the tunnel.
python3 - "$URL" "$ROOT/.env" <<'PY'
import pathlib, re, sys
url, env_path = sys.argv[1], pathlib.Path(sys.argv[2])
if not env_path.exists():
    sys.exit(".env not found -- copy .env.example to .env first")
text = env_path.read_text()
updated, count = re.subn(r"^PUBLIC_BASE_URL=.*$", f"PUBLIC_BASE_URL={url}", text, flags=re.M)
if count != 1:
    sys.exit(f"expected exactly one PUBLIC_BASE_URL line in .env, found {count}")
env_path.write_text(updated)
PY

# 2. Recreate (not restart) so the container actually picks up the new value.
echo "Recreating the backend so it picks up the new URL ..."
(cd "$ROOT" && docker compose up -d --force-recreate backend >/dev/null 2>&1) || {
  echo "Could not recreate the backend container. Run this yourself:" >&2
  echo "  docker compose up -d --force-recreate backend" >&2
}

cat <<EOF

  Tunnel is up:  $URL
  (also saved to .tunnel-url while this is running)

  3. Paste these into the Twilio console, on your number,
     Voice Configuration -- both as HTTP POST:

       A call comes in      $URL/webhook/incoming-call
       Call status changes  $URL/webhook/call-status

  Dashboard: http://localhost:\${FRONTEND_PORT:-5173}

  Ctrl-C to close the tunnel.

EOF

wait "$TUNNEL_PID"
