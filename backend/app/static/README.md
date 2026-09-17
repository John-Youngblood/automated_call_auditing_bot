# Static assets

`greeting.mp3` is the real recorded greeting. It is also the **prompt** — it
plays inside `<Gather>`, so it has to ask the caller to say why they are
calling, or they will sit in silence waiting for a person.

To replace it, drop in any MP3 or WAV that Twilio can fetch over HTTPS, or set
`GREETING_AUDIO_URL` to a hosted file. In production, prefer a CDN so Twilio
fetches the audio from somewhere other than your API.
