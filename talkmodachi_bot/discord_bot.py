from __future__ import annotations

import asyncio
import logging
import os
import random
import tempfile
from pathlib import Path

import discord
from discord import app_commands

from .message_cleaner import clean_message
from .render_client import RendererClient
from .storage import Storage
from .voices import BUILTIN_VOICES, VoiceParams


LOGGER = logging.getLogger(__name__)


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


class GuildPlayer:
    def __init__(self, bot: "TalkmodachiBot", guild_id: int) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.queue: asyncio.Queue[tuple[str, VoiceParams, discord.abc.Messageable | None]] = asyncio.Queue(maxsize=20)
        self.task: asyncio.Task[None] | None = None
        self.voice_client: discord.VoiceClient | None = None

    async def connect(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        if self.voice_client and self.voice_client.is_connected():
            if self.voice_client.channel != channel:
                await self.voice_client.move_to(channel)
            return
        self.voice_client = await channel.connect(self_deaf=True)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def disconnect(self) -> None:
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect(force=True)
        self.voice_client = None

    async def enqueue(self, text: str, voice: VoiceParams, reply_to: discord.abc.Messageable | None = None) -> bool:
        try:
            self.queue.put_nowait((text, voice, reply_to))
        except asyncio.QueueFull:
            return False
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())
        return True

    def clear(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()

    async def _run(self) -> None:
        while True:
            text, voice, reply_to = await self.queue.get()
            try:
                if self.voice_client is None or not self.voice_client.is_connected():
                    continue
                audio = await self.bot.renderer.render(text, voice)
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as file:
                    file.write(audio)
                    path = Path(file.name)
                await self._play_file(path)
            except Exception as error:
                LOGGER.exception("TTS playback job failed")
                if reply_to is not None:
                    await reply_to.send(f"TTS failed: {error}", delete_after=10)
            finally:
                self.queue.task_done()

    async def _play_file(self, path: Path) -> None:
        done = asyncio.Event()

        def after(error: Exception | None) -> None:
            if error:
                LOGGER.warning("Discord playback failed", exc_info=error)
            self.bot.loop.call_soon_threadsafe(done.set)

        source = discord.FFmpegPCMAudio(str(path))
        assert self.voice_client is not None
        try:
            self.voice_client.play(source, after=after)
        except Exception:
            source.cleanup()
            raise
        await done.wait()
        path.unlink(missing_ok=True)


class TalkmodachiBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.storage = Storage(os.environ.get("DATABASE_PATH", "/data/talkmodachi.sqlite3"))
        self.renderer = RendererClient(os.environ.get("RENDERER_URL", "http://tts-worker:8080"))
        self.players: dict[int, GuildPlayer] = {}
        self.sync_commands = env_bool("SYNC_COMMANDS_ON_START", True)

    async def setup_hook(self) -> None:
        register_commands(self)
        if self.sync_commands:
            await self.tree.sync()

    async def close(self) -> None:
        await self.renderer.close()
        self.storage.close()
        await super().close()

    def player_for(self, guild_id: int) -> GuildPlayer:
        player = self.players.get(guild_id)
        if player is None:
            player = GuildPlayer(self, guild_id)
            self.players[guild_id] = player
        return player

    async def on_ready(self) -> None:
        assert self.user is not None
        print(f"Logged in as {self.user} ({self.user.id})")

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return

        settings = self.storage.get_guild_settings(message.guild.id)
        if settings.ignore_bots and message.author.bot:
            return

        author_vc = message.author.voice.channel if message.author.voice else None
        in_setup_channel = settings.setup_channel_id == message.channel.id
        in_text_voice = bool(
            settings.text_in_voice
            and author_vc
            and author_vc.id == getattr(message.channel, "id", None)
        )
        if not in_setup_channel and not in_text_voice:
            return
        if message.content.startswith(("/", "!", "-", ".")):
            return
        if settings.required_role_id and isinstance(message.author, discord.Member):
            if settings.required_role_id not in {role.id for role in message.author.roles}:
                return

        if author_vc is None:
            return

        player = self.player_for(message.guild.id)
        if player.voice_client is None or not player.voice_client.is_connected():
            if not settings.autojoin:
                return
            await player.connect(author_vc)
        elif settings.require_same_vc and player.voice_client.channel != author_vc:
            return

        text = clean_message(
            message.content,
            attachments=[attachment.filename for attachment in message.attachments],
            skip_emoji=settings.skip_emoji,
            required_prefix=settings.required_prefix,
            announce_name=message.author.display_name if settings.announce_name else None,
        )
        if not text:
            return
        text = text[: settings.max_message_length]

        voice_id = self.storage.get_user_default(message.guild.id, message.author.id) or settings.default_voice_id
        voice = self.storage.resolve_voice(voice_id, message.guild.id, message.author.id)
        queued = await player.enqueue(text, voice, message.channel)
        if not queued:
            await message.add_reaction("⏳")


def register_commands(bot: TalkmodachiBot) -> None:
    @bot.tree.command(name="setup", description="Set the text channel Talkmodachi reads from.")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild is not None
        bot.storage.set_guild_value(interaction.guild.id, "setup_channel_id", channel.id)
        await interaction.response.send_message(f"Talkmodachi will read messages from {channel.mention}.", ephemeral=True)

    @bot.tree.command(name="join", description="Join your voice channel.")
    async def join(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await interaction.response.send_message("You need to be in a voice channel.", ephemeral=True)
            return
        await bot.player_for(interaction.guild.id).connect(member.voice.channel)
        await interaction.response.send_message("Joined. Type normally in the setup channel.", ephemeral=True)

    @bot.tree.command(name="leave", description="Leave the current voice channel.")
    async def leave(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await bot.player_for(interaction.guild.id).disconnect()
        await interaction.response.send_message("Left voice channel.", ephemeral=True)

    @bot.tree.command(name="skip", description="Clear queued TTS and stop current playback.")
    async def skip(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        bot.player_for(interaction.guild.id).clear()
        await interaction.response.send_message("Cleared the queue.", ephemeral=True)

    @bot.tree.command(name="settings", description="Show Talkmodachi settings for this server.")
    async def settings(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        row = bot.storage.get_guild_settings(interaction.guild.id)
        setup_channel = f"<#{row.setup_channel_id}>" if row.setup_channel_id else "not set"
        await interaction.response.send_message(
            "\n".join(
                [
                    f"Setup channel: {setup_channel}",
                    f"Autojoin: {row.autojoin}",
                    f"Require same VC: {row.require_same_vc}",
                    f"Text-in-voice: {row.text_in_voice}",
                    f"Default voice: {row.default_voice_id}",
                    f"Max message length: {row.max_message_length}",
                ]
            ),
            ephemeral=True,
        )

    voice_group = app_commands.Group(name="voice", description="Manage Talkmodachi voices.")

    @voice_group.command(name="list", description="List available voices.")
    async def voice_list(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        voices = bot.storage.list_voices(interaction.guild.id, interaction.user.id)
        rendered = ", ".join(f"`{voice_id}`" for voice_id, _ in voices[:40])
        await interaction.response.send_message(rendered or "No voices available.", ephemeral=True)

    @voice_group.command(name="use", description="Use a voice for your messages.")
    async def voice_use(interaction: discord.Interaction, voice_id: str) -> None:
        assert interaction.guild is not None
        bot.storage.set_user_default(interaction.guild.id, interaction.user.id, voice_id)
        await interaction.response.send_message(f"Your voice is now `{voice_id}`.", ephemeral=True)

    @voice_group.command(name="default", description="Set the server default voice.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def voice_default(interaction: discord.Interaction, voice_id: str) -> None:
        assert interaction.guild is not None
        bot.storage.set_guild_value(interaction.guild.id, "default_voice_id", voice_id)
        await interaction.response.send_message(f"Server default voice is now `{voice_id}`.", ephemeral=True)

    @voice_group.command(name="random", description="Use a random built-in voice.")
    async def voice_random(interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        voice_id = random.choice(list(BUILTIN_VOICES))
        bot.storage.set_user_default(interaction.guild.id, interaction.user.id, voice_id)
        await interaction.response.send_message(f"Your voice is now `{voice_id}`.", ephemeral=True)

    @voice_group.command(name="save", description="Save a custom voice.")
    async def voice_save(
        interaction: discord.Interaction,
        name: str,
        pitch: app_commands.Range[int, 0, 100] = 50,
        speed: app_commands.Range[int, 0, 100] = 50,
        quality: app_commands.Range[int, 0, 100] = 50,
        tone: app_commands.Range[int, 0, 100] = 50,
        accent: app_commands.Range[int, 0, 100] = 50,
        intonation: app_commands.Range[int, 1, 4] = 1,
        lang: str = "useng",
    ) -> None:
        assert interaction.guild is not None
        voice = VoiceParams(pitch=pitch, speed=speed, quality=quality, tone=tone, accent=accent, intonation=intonation, lang=lang)
        voice.validate()
        voice_id = name.lower().replace(" ", "-")[:32]
        bot.storage.save_voice(
            voice_id=voice_id,
            name=name,
            voice=voice,
            guild_id=interaction.guild.id,
            owner_user_id=interaction.user.id,
        )
        bot.storage.set_user_default(interaction.guild.id, interaction.user.id, voice_id)
        await interaction.response.send_message(f"Saved and selected `{voice_id}`.", ephemeral=True)

    @voice_group.command(name="delete", description="Delete one of your custom voices.")
    async def voice_delete(interaction: discord.Interaction, voice_id: str) -> None:
        assert interaction.guild is not None
        deleted = bot.storage.delete_voice(voice_id=voice_id, guild_id=interaction.guild.id, owner_user_id=interaction.user.id)
        await interaction.response.send_message("Deleted." if deleted else "No matching voice found.", ephemeral=True)

    bot.tree.add_command(voice_group)


def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is required")
    load_opus()
    bot = TalkmodachiBot()
    bot.run(token)


def load_opus() -> None:
    if discord.opus.is_loaded():
        return
    candidates = [
        os.environ.get("DISCORD_OPUS_LIBRARY"),
        "libopus.so.0",
        "libopus.so",
        "/usr/lib/libopus.so.0",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            discord.opus.load_opus(candidate)
        except OSError:
            continue
        if discord.opus.is_loaded():
            return
    raise RuntimeError("Discord opus library could not be loaded")


if __name__ == "__main__":
    main()
