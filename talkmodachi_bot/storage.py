from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .voices import BUILTIN_VOICES, VoiceParams


@dataclass(frozen=True)
class GuildSettings:
    guild_id: int
    setup_channel_id: int | None = None
    autojoin: bool = True
    require_same_vc: bool = True
    ignore_bots: bool = True
    required_prefix: str | None = None
    required_role_id: int | None = None
    max_message_length: int = 200
    text_in_voice: bool = True
    skip_emoji: bool = False
    announce_name: bool = True
    default_voice_id: str = "adultf"


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    setup_channel_id INTEGER,
                    autojoin INTEGER NOT NULL DEFAULT 1,
                    require_same_vc INTEGER NOT NULL DEFAULT 1,
                    ignore_bots INTEGER NOT NULL DEFAULT 1,
                    required_prefix TEXT,
                    required_role_id INTEGER,
                    max_message_length INTEGER NOT NULL DEFAULT 200,
                    text_in_voice INTEGER NOT NULL DEFAULT 1,
                    skip_emoji INTEGER NOT NULL DEFAULT 0,
                    announce_name INTEGER NOT NULL DEFAULT 1,
                    default_voice_id TEXT NOT NULL DEFAULT 'adultf'
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS voice_presets (
                    id TEXT NOT NULL,
                    guild_id INTEGER,
                    owner_user_id INTEGER,
                    name TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(id, guild_id, owner_user_id)
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_settings (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    default_voice_id TEXT,
                    PRIMARY KEY(guild_id, user_id)
                )
                """
            )

    def get_guild_settings(self, guild_id: int) -> GuildSettings:
        with self.lock:
            row = self.conn.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)).fetchone()
            if row is None:
                return GuildSettings(guild_id=guild_id)
            return GuildSettings(
                guild_id=guild_id,
                setup_channel_id=row["setup_channel_id"],
                autojoin=bool(row["autojoin"]),
                require_same_vc=bool(row["require_same_vc"]),
                ignore_bots=bool(row["ignore_bots"]),
                required_prefix=row["required_prefix"],
                required_role_id=row["required_role_id"],
                max_message_length=int(row["max_message_length"]),
                text_in_voice=bool(row["text_in_voice"]),
                skip_emoji=bool(row["skip_emoji"]),
                announce_name=bool(row["announce_name"]),
                default_voice_id=row["default_voice_id"],
            )

    def set_guild_value(self, guild_id: int, column: str, value: object) -> None:
        allowed = {
            "setup_channel_id",
            "autojoin",
            "require_same_vc",
            "ignore_bots",
            "required_prefix",
            "required_role_id",
            "max_message_length",
            "text_in_voice",
            "skip_emoji",
            "announce_name",
            "default_voice_id",
        }
        if column not in allowed:
            raise ValueError(f"Unsupported guild setting: {column}")
        with self.lock, self.conn:
            self.conn.execute("INSERT OR IGNORE INTO guild_settings(guild_id) VALUES (?)", (guild_id,))
            self.conn.execute(f"UPDATE guild_settings SET {column} = ? WHERE guild_id = ?", (value, guild_id))

    def save_voice(
        self,
        *,
        voice_id: str,
        name: str,
        voice: VoiceParams,
        guild_id: int | None,
        owner_user_id: int | None,
    ) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO voice_presets(id, guild_id, owner_user_id, name, params_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (voice_id, guild_id, owner_user_id, name, json.dumps(voice.to_dict()), int(time.time())),
            )

    def delete_voice(self, *, voice_id: str, guild_id: int | None, owner_user_id: int | None) -> bool:
        with self.lock, self.conn:
            cur = self.conn.execute(
                "DELETE FROM voice_presets WHERE id = ? AND guild_id IS ? AND owner_user_id IS ?",
                (voice_id, guild_id, owner_user_id),
            )
            return cur.rowcount > 0

    def set_user_default(self, guild_id: int, user_id: int, voice_id: str | None) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO user_settings(guild_id, user_id, default_voice_id)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET default_voice_id = excluded.default_voice_id
                """,
                (guild_id, user_id, voice_id),
            )

    def get_user_default(self, guild_id: int, user_id: int) -> str | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT default_voice_id FROM user_settings WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            return None if row is None else row["default_voice_id"]

    def resolve_voice(self, voice_id: str | None, guild_id: int | None = None, user_id: int | None = None) -> VoiceParams:
        voice_id = voice_id or "adultf"
        if voice_id in BUILTIN_VOICES:
            return BUILTIN_VOICES[voice_id]
        with self.lock:
            row = self.conn.execute(
                """
                SELECT params_json FROM voice_presets
                WHERE id = ? AND (
                    (owner_user_id = ? AND guild_id = ?)
                    OR (owner_user_id IS NULL AND guild_id = ?)
                    OR (owner_user_id = ? AND guild_id IS NULL)
                )
                ORDER BY owner_user_id IS NOT NULL DESC, guild_id IS NOT NULL DESC
                LIMIT 1
                """,
                (voice_id, user_id, guild_id, guild_id, user_id),
            ).fetchone()
        if row is None:
            return BUILTIN_VOICES["adultf"]
        return VoiceParams.from_mapping(json.loads(row["params_json"]))

    def list_voices(self, guild_id: int, user_id: int) -> list[tuple[str, str]]:
        voices = [(voice_id, voice_id) for voice_id in BUILTIN_VOICES]
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT id, name FROM voice_presets
                WHERE (guild_id = ? AND owner_user_id IS NULL)
                   OR (guild_id = ? AND owner_user_id = ?)
                   OR (guild_id IS NULL AND owner_user_id = ?)
                ORDER BY name
                """,
                (guild_id, guild_id, user_id, user_id),
            ).fetchall()
        voices.extend((row["id"], row["name"]) for row in rows)
        return voices

