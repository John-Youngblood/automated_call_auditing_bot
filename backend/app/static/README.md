# Static assets

`greeting.mp3` is a generated **silent** placeholder so the `<Play>` verb in the
webhook response resolves during local development. Regenerate it with:

```bash
python scripts/make_placeholder_greeting.py --seconds 3
```

Replace it with a real recording, or set `GREETING_AUDIO_URL` to a hosted file.
In production prefer hosting the audio on a CDN so the telecom provider fetches
greetings from somewhere other than your API.
