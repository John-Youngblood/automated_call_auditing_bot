# Static assets

Everything here is served at `/static/`, and anything a caller hears can be
pointed at by **filename** rather than a full URL:

    GREETING_AUDIO_URL=greeting.mp3
    HOLD_MUSIC_URL=h3_podcast_theme.mp3

That form is rebuilt against `PUBLIC_BASE_URL` on every call, which is the
point: in development the tunnel hostname rotates, and `.env` cannot
interpolate, so an absolute URL pasted there goes stale on every restart.
An absolute URL still works and is what you want in production — put the
audio on a CDN so Twilio is not pulling megabytes through your API.

## greeting.mp3

The real recorded greeting, and also the **prompt** — it plays inside
`<Gather>`, so it has to ask the caller to say why they are calling, or they
will sit in silence waiting for a person. Leaving `GREETING_AUDIO_URL` blank
falls back to this file.

## Hold music

`h3_podcast_theme.mp3` and `h3h3_production_theme.mp3` are here to choose
between. Neither is used unless `HOLD_MUSIC_URL` names one — blank means
Twilio's own classical playlist, not silence.

Both are ~2 MB. Twilio caches a `waitUrl` it fetches with `GET` (which is what
`<Enqueue>` sends), so the file is pulled once rather than once per loop per
caller — but on a CDN in production that stops mattering either way.
