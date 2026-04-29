# Deploying on coah

Target server: `coah`, accessed through `ssh-mcp`.

## Layout

- App path: `/home/cole/talkmodachi-bot`
- ROM path: `/home/cole/talkmodachi-bot/roms`
- Data volume: Docker named volume `newproject8_bot-data`
- Renderer cache: Docker named volume `newproject8_renderer-cache`

## First Deploy

1. Copy this repo to `/home/cole/talkmodachi-bot`.
2. Create `/home/cole/talkmodachi-bot/.env` from `.env.example`.
3. Put `US.cxi` in `/home/cole/talkmodachi-bot/roms/`.
4. Run `docker compose up --build -d`.
5. Check `docker compose logs -f tts-worker bot`.

## Runtime Notes

- Start with `TALKMODACHI_WORKER_ROMS=US` and `TALKMODACHI_US_WORKERS=1`.
- Increase workers only after measuring RAM and CPU per warm Citra instance.
- `TALKMODACHI_MAX_INFLIGHT_RENDERS` bounds unique cache-miss renders; duplicates wait on the same render and return from cache.
- The renderer should stay private on `127.0.0.1:8080`; the bot reaches it through Docker networking.
- If the renderer health endpoint reports a missing ROM, the bot can start but TTS playback will fail until the ROM is mounted.
