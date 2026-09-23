# Static assets

Everything here is served at `/static/`, and anything a caller hears can be
pointed at by **filename** rather than a full URL:

    GREETING_AUDIO_URL=greeting.mp3
    HOLD_MUSIC_URL=h3_podcast_theme.mp3

A filename that isn't in this folder is ignored and the prompt's text is
spoken instead — the app logs a warning at startup naming it.

That form is rebuilt against `PUBLIC_BASE_URL` on every call, which is the
point: in development the tunnel hostname rotates, and `.env` cannot
interpolate, so an absolute URL pasted there goes stale on every restart.
An absolute URL still works and is what you want in production — put the
audio on a CDN so Twilio is not pulling megabytes through your API.
