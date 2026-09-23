#!/usr/bin/env python3
"""Copy your .env settings to Cloud Run: shared settings, and changed secrets.

    make push-env

Two kinds of setting are sent, and everything else is left alone:

* Settings in SHARED, as plain env vars. Others are tied to where they run --
  your local PUBLIC_BASE_URL points at a tunnel, and pushing it would break
  every call in production.
* Secrets the service already reads from Secret Manager (the dashboard password
  and Twilio credentials), when your .env value differs. Values are never
  printed.

Shows what would change and asks first, because every update starts a new
revision, which restarts the service.

Uses --update-env-vars, which merges. Never --env-vars-file: that deletes every
variable not in the file, including APP_ENV and PUBLIC_BASE_URL.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: How the show sounds and behaves, so production should match what you tested
#: locally. An allowlist on purpose: a secret added to .env later can never be
#: swept up by accident.
SHARED = (
    "HOST_PHONE_NUMBER",
    "TWILIO_PHONE_NUMBER",
    "TTS_VOICE",
    "GREETING_AUDIO_URL",
    "GREETING_MESSAGE",
    "HOLD_MUSIC_URL",
    "REJECT_AUDIO_URL",
    "REJECT_MESSAGE",
    "CLOSED_LINE_AUDIO_URL",
    "CLOSED_LINE_MESSAGE",
    "CLOSING_AUDIO_URL",
    "CLOSING_MESSAGE",
    "SPEECH_MODEL",
    "SPEECH_LANGUAGE",
    "SPEECH_TIMEOUT_SECONDS",
    "HOLD_QUEUE_NAME",
    "LINE_OPEN_ON_START",
    "RECONCILE_ON_STARTUP",
    "TWILIO_API_TIMEOUT_SECONDS",
    "CALL_HISTORY_SIZE",
    "FRONTEND_QUEUE_MAX",
)

#: Mirrors MIN_PASSWORD_LENGTH in backend/app/services/sessions.py. Checked here
#: so a short password fails now, not as a revision that refuses to start.
MIN_PASSWORD_LENGTH = 8


def gcloud(*args: str, stdin: str | None = None) -> str:
    """Run gcloud and return stdout, or exit with its error."""
    result = subprocess.run(
        ["gcloud", *args], input=stdin, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        sys.exit(result.stderr.strip() or f"gcloud {args[0]} failed")
    return result.stdout


def read_env(path: Path) -> dict[str, str]:
    """KEY=VALUE lines, with surrounding quotes stripped as the app does."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def deployed(service: str, region: str) -> tuple[dict[str, str], dict[str, str]]:
    """The live service's plain env vars, and which secret backs each secret var."""
    spec = json.loads(gcloud("run", "services", "describe", service, f"--region={region}", "--format=json"))
    env = spec["spec"]["template"]["spec"]["containers"][0].get("env", [])
    plain = {e["name"]: e["value"] for e in env if "value" in e}
    secrets = {e["name"]: e["valueFrom"]["secretKeyRef"]["name"] for e in env if "valueFrom" in e}
    return plain, secrets


def show(value: str | None) -> str:
    if value is None:
        return "(not set -- the app's default)"
    if value == "":
        return "(blank)"
    return value if len(value) <= 72 else value[:69] + "..."


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", default="call-screener")
    parser.add_argument("--region", default="us-west1")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    env_file = ROOT / ".env"
    if not env_file.is_file():
        sys.exit(".env not found -- nothing to push")

    local = read_env(env_file)
    remote, bound = deployed(args.service, args.region)

    # Unset on Cloud Run and blank locally both mean "the app's default", so
    # they count as the same and do not trigger a pointless restart.
    env_changes = {k: local[k] for k in SHARED if k in local and remote.get(k, "") != local[k]}

    # Only secrets the service already uses, and only with a value in .env --
    # blank locally means "leave production's alone", not "erase it".
    secret_changes = [
        key
        for key, secret in bound.items()
        if local.get(key)
        and gcloud("secrets", "versions", "access", "latest", f"--secret={secret}") != local[key]
    ]

    if not env_changes and not secret_changes:
        print(f"{args.service} already matches .env -- nothing to push.")
        return

    print(f"Changes to {args.service} ({args.region}):\n")
    for key, value in env_changes.items():
        print(f"  {key}\n    now: {show(remote.get(key))}\n    new: {show(value)}\n")
    for key in secret_changes:
        print(f"  {key}  (secret, value hidden)\n    will be updated to match .env\n")

    if "DASHBOARD_PASSWORD" in secret_changes and len(local["DASHBOARD_PASSWORD"]) < MIN_PASSWORD_LENGTH:
        sys.exit(f"DASHBOARD_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters.")

    print("Pushing restarts the service and signs everyone out. Avoid it mid-show.")
    if not args.yes and input("Push these? [y/N] ").strip().lower() != "y":
        print("Nothing pushed.")
        return

    command = ["run", "services", "update", args.service, f"--region={args.region}"]

    if env_changes:
        # The messages contain commas, which gcloud would read as separators.
        # ^X^ tells it to split on X instead -- pick one that appears nowhere.
        delim = next((c for c in "|~@%" if not any(c in k + v for k, v in env_changes.items())), None)
        if delim is None:
            sys.exit("Every candidate delimiter appears in a value; push these by hand.")
        pairs = delim.join(f"{k}={v}" for k, v in env_changes.items())
        command.append(f"--update-env-vars=^{delim}^{pairs}")

    if secret_changes:
        # Pinned to the version just added, not ":latest". A running instance
        # reads a secret once, at startup, and re-stating ":latest" changes
        # nothing in the config -- so no new revision, and the old value lives
        # on. A version number is a real change, which forces the restart.
        refs = []
        for key in secret_changes:
            name = gcloud(
                "secrets", "versions", "add", bound[key],
                "--data-file=-", "--format=value(name)",
                stdin=local[key],
            ).strip()
            refs.append(f"{key}={bound[key]}:{name.rsplit('/', 1)[-1]}")
        command.append(f"--update-secrets={','.join(refs)}")

    sys.exit(subprocess.run(["gcloud", *command], check=False).returncode)


if __name__ == "__main__":
    main()
