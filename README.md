# Talkmodachi Bot

Discord-first Tomodachi Life TTS using Talkmodachi's patched Citra renderer.

This fork keeps the original Talkmodachi engine work and turns it into a server bot: admins run `/setup`, the bot joins voice with `/join`, then normal messages in the configured channel are spoken with Tomodachi-style voices.

## What Changed

- Discord bot UX inspired by Discord-TTS/Bot: `/setup`, `/join`, `/leave`, `/skip`, `/settings`, and `/voice ...`.
- Warm renderer service instead of per-request Citra startup.
- File cache keyed by text, voice params, language, mode, and engine version, with duplicate in-flight renders collapsed.
- SQLite storage for guild settings and user/guild voice presets.
- Isolated warm Citra workers with fixed UDP ports, native-resolution software rendering, dummy SDL audio/video, timeout restart, and lower idle CPU in the game patch wait loop.
- Direct WAV wrapping for raw PCM instead of `pydub`.

## Local Run

1. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`.
2. Put patched ROMs in `roms/`, starting with `roms/US.cxi`.
3. Run `docker compose up --build`.

The renderer exposes health on the host at `http://127.0.0.1:18080/health` by default. Override `RENDERER_HOST_PORT` if that port is already in use.

## Commands

- `/setup #channel` sets the text channel to read from.
- `/join` joins your current voice channel.
- `/leave` leaves voice.
- `/skip` clears queued TTS and stops current playback.
- `/settings` shows current server settings.
- `/voice list` lists built-in and saved voices.
- `/voice save` saves a custom voice from Talkmodachi parameters.
- `/voice use` selects your voice.
- `/voice default` sets the server default.
- `/voice random` picks a random built-in voice.
- `/voice delete` deletes one of your custom voices.

## Upstream Credit

This project is based on Talkmodachi by dylanpdx: https://github.com/dylanpdx/talkmodachi

Talkmodachi uses a patched Tomodachi Life build and custom Citra fork to render speech. You need legally obtained and patched CXI files for the regions you enable.
