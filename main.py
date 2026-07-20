#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════════════
  Telegram Music Bot - Persian UI
  Python 3.12  •  aiogram 3.29  •  SQLite (async via aiosqlite)
  Polling mode  •  Railway-ready  •  Production-grade
═══════════════════════════════════════════════════════════════════════

  Environment variables required:
      BOT_TOKEN       — your bot token from @BotFather
      ADMIN_ID        — numeric Telegram user id of the admin
      FORCE_CHANNEL   — channel username (e.g. @mychannel) the user
                        must join before using the bot
═══════════════════════════════════════════════════════════════════════
"""

import asyncio
import logging
import os
import sys
import traceback
from datetime import datetime
from enum import Enum
from typing import Optional, List

import aiosqlite
import aiogram
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ── ButtonStyle compatibility shim ──────────────────────────────────
# The real `ButtonStyle` enum lives in `aiogram.types` only on
# aiogram >= 3.13.  For older versions we fall back to a local
# string-based enum that has the SAME interface (ButtonStyle.PRIMARY,
# ButtonStyle.SUCCESS, etc.) so the rest of the code is unchanged.
try:
    from aiogram.types import ButtonStyle  # type: ignore[attr-defined]
    _BUTTON_STYLE_NATIVE = True
except ImportError:
    try:
        from aiogram.methods.types import ButtonStyle  # type: ignore[attr-defined]
        _BUTTON_STYLE_NATIVE = True
    except ImportError:
        class ButtonStyle(str, Enum):
            """Local fallback — matches aiogram 3.13+ API."""
            PRIMARY = "primary"
            SUCCESS = "success"
            DANGER = "danger"
            SECONDARY = "secondary"
        _BUTTON_STYLE_NATIVE = False

# Silence noisy aiogram internal loggers (we keep our own)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
logging.getLogger("aiogram.dispatcher").setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "")
DB_PATH = "music.db"

if not BOT_TOKEN:
    print("FATAL: BOT_TOKEN environment variable is required.", file=sys.stderr)
    sys.exit(1)
if ADMIN_ID == 0:
    print("WARNING: ADMIN_ID is not set (admin panel will be inaccessible).",
          file=sys.stderr)

# Show aiogram version and ButtonStyle support status
print("=" * 60)
print(f"  aiogram version: {aiogram.__version__}")
if _BUTTON_STYLE_NATIVE:
    print("  ButtonStyle: native (coloured buttons will render)")
else:
    print("  ButtonStyle: LOCAL FALLBACK — coloured buttons require"
          " aiogram>=3.13")
    print("  Buttons will work, but the visual style will not be sent"
          " to Telegram.")
    print("  -> Upgrade with:  pip install --upgrade 'aiogram>=3.13'")
print("=" * 60)

# ═══════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("music_bot")


# ═══════════════════════════════════════════════════════════════════
#  DATABASE LAYER
# ═══════════════════════════════════════════════════════════════════
async def init_db() -> None:
    """Create all required tables and seed default categories."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")

        # ── users ────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT,
                join_date  TEXT
            )
        """)

        # ── songs ────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS songs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                song_name       TEXT NOT NULL,
                artist          TEXT,
                category        TEXT,
                cover_file_id   TEXT,
                audio_file_id   TEXT UNIQUE,
                uploader_id     INTEGER,
                downloads       INTEGER DEFAULT 0,
                views           INTEGER DEFAULT 0,
                upload_date     TEXT
            )
        """)

        # ── favorites ────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id    INTEGER,
                song_id    INTEGER,
                added_date TEXT,
                PRIMARY KEY (user_id, song_id)
            )
        """)

        # ── categories ───────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
        """)

        # ── downloads ────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER,
                song_id       INTEGER,
                download_date TEXT
            )
        """)

        # ── banned ───────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS banned (
                user_id  INTEGER PRIMARY KEY,
                ban_date TEXT,
                reason   TEXT
            )
        """)

        # ── settings ─────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Seed default categories (idempotent)
        defaults = ["Pop", "Rock", "Hip-Hop", "Jazz",
                    "Classic", "Persian", "Remix", "Other"]
        for cat in defaults:
            try:
                await db.execute(
                    "INSERT INTO categories (name) VALUES (?)", (cat,)
                )
            except aiosqlite.IntegrityError:
                pass

        await db.commit()
    logger.info("Database initialised at %s", DB_PATH)


# ── user helpers ────────────────────────────────────────────────────
async def add_user(user_id: int, username: str, first_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users "
            "(user_id, username, first_name, join_date) VALUES (?,?,?,?)",
            (user_id, username, first_name, datetime.now().isoformat()),
        )
        await db.commit()


async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM banned WHERE user_id = ?", (user_id,)
        )
        return (await cur.fetchone()) is not None


async def add_ban(user_id: int, reason: str = "") -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO banned "
            "(user_id, ban_date, reason) VALUES (?,?,?)",
            (user_id, datetime.now().isoformat(), reason),
        )
        await db.commit()


async def remove_ban(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM banned WHERE user_id = ?", (user_id,))
        await db.commit()


async def get_user_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        return (await cur.fetchone())[0]


async def get_today_users() -> int:
    today = datetime.now().date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM users WHERE join_date LIKE ?",
            (f"{today}%",),
        )
        return (await cur.fetchone())[0]


async def get_all_users() -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        return [r[0] for r in await cur.fetchall()]


# ── song helpers ────────────────────────────────────────────────────
async def add_song(song_name, artist, category, cover_file_id,
                   audio_file_id, uploader_id) -> Optional[int]:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO songs "
                "(song_name,artist,category,cover_file_id,audio_file_id,"
                " uploader_id,downloads,views,upload_date) "
                "VALUES (?,?,?,?,?,?,0,0,?)",
                (song_name, artist, category, cover_file_id,
                 audio_file_id, uploader_id, datetime.now().isoformat()),
            )
            await db.commit()
            return cur.lastrowid
    except aiosqlite.IntegrityError:
        return None


async def get_song_by_id(song_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM songs WHERE id = ?", (song_id,))
        return await cur.fetchone()


async def search_songs(query: str, limit: int = 100):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM songs WHERE LOWER(song_name) LIKE ? "
            "COLLATE NOCASE ORDER BY downloads DESC LIMIT ?",
            (f"%{query.lower()}%", limit),
        )
        return await cur.fetchall()


async def search_by_artist(query: str, limit: int = 100):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM songs WHERE LOWER(artist) LIKE ? "
            "COLLATE NOCASE ORDER BY downloads DESC LIMIT ?",
            (f"%{query.lower()}%", limit),
        )
        return await cur.fetchall()


async def search_by_category(category: str, limit: int = 100):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM songs WHERE LOWER(category) = LOWER(?) "
            "ORDER BY downloads DESC LIMIT ?",
            (category, limit),
        )
        return await cur.fetchall()


async def increment_downloads(song_id: int, user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE songs SET downloads = downloads + 1 WHERE id = ?",
            (song_id,),
        )
        await db.execute(
            "INSERT INTO downloads (user_id, song_id, download_date) "
            "VALUES (?,?,?)",
            (user_id, song_id, datetime.now().isoformat()),
        )
        await db.commit()


async def increment_views(song_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE songs SET views = views + 1 WHERE id = ?", (song_id,)
        )
        await db.commit()


async def get_top_downloads(limit: int = 20):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM songs ORDER BY downloads DESC LIMIT ?", (limit,)
        )
        return await cur.fetchall()


async def get_random_song():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM songs ORDER BY RANDOM() LIMIT 1")
        return await cur.fetchone()


async def get_user_uploads(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM songs WHERE uploader_id = ? "
            "ORDER BY upload_date DESC",
            (user_id,),
        )
        return await cur.fetchall()


async def delete_song(song_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM songs WHERE id = ?", (song_id,))
        await db.execute(
            "DELETE FROM favorites WHERE song_id = ?", (song_id,)
        )
        await db.commit()
        return cur.rowcount > 0


async def get_song_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM songs")
        return (await cur.fetchone())[0]


async def get_download_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM downloads")
        return (await cur.fetchone())[0]


async def get_today_uploads() -> int:
    today = datetime.now().date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM songs WHERE upload_date LIKE ?",
            (f"{today}%",),
        )
        return (await cur.fetchone())[0]


# ── favorites helpers ───────────────────────────────────────────────
async def add_favorite(user_id: int, song_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO favorites "
            "(user_id, song_id, added_date) VALUES (?,?,?)",
            (user_id, song_id, datetime.now().isoformat()),
        )
        await db.commit()


async def remove_favorite(user_id: int, song_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM favorites WHERE user_id = ? AND song_id = ?",
            (user_id, song_id),
        )
        await db.commit()


async def get_user_favorites(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT s.* FROM songs s "
            "JOIN favorites f ON s.id = f.song_id "
            "WHERE f.user_id = ? ORDER BY f.added_date DESC",
            (user_id,),
        )
        return await cur.fetchall()


async def is_favorite(user_id: int, song_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND song_id = ?",
            (user_id, song_id),
        )
        return (await cur.fetchone()) is not None


# ── categories helpers ──────────────────────────────────────────────
async def get_categories() -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT name FROM categories ORDER BY name")
        return [r[0] for r in await cur.fetchall()]


async def add_category(name: str) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO categories (name) VALUES (?)", (name,))
            await db.commit()
            return True
    except aiosqlite.IntegrityError:
        return False


async def delete_category(name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM categories WHERE name = ?", (name,)
        )
        await db.commit()
        return cur.rowcount > 0


# ═══════════════════════════════════════════════════════════════════
#  FSM STATES
# ═══════════════════════════════════════════════════════════════════
class UploadStates(StatesGroup):
    song_name = State()
    artist = State()
    category = State()
    cover = State()
    audio = State()


class BroadcastStates(StatesGroup):
    text = State()


class AddCategoryStates(StatesGroup):
    name = State()


class DeleteSongStates(StatesGroup):
    song_id = State()


class BanStates(StatesGroup):
    user_id = State()


class UnbanStates(StatesGroup):
    user_id = State()


class SearchStates(StatesGroup):
    query = State()


# ═══════════════════════════════════════════════════════════════════
#  KEYBOARDS  (all with ButtonStyle coloured buttons)
# ═══════════════════════════════════════════════════════════════════
def _normalize_channel(channel: str) -> str:
    channel = (channel or "").strip()
    if channel.startswith("https://t.me/"):
        channel = channel.split("https://t.me/", 1)[1]
    if channel.startswith("t.me/"):
        channel = channel.split("t.me/", 1)[1]
    return channel.lstrip("@")


def force_join_keyboard() -> InlineKeyboardBuilder:
    """Force-join screen: Join Channel (PRIMARY) + Check (SUCCESS)."""
    channel = _normalize_channel(FORCE_CHANNEL)
    url = f"https://t.me/{channel}" if channel else "https://t.me/"
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="📢 Join Channel",
            url=url,
            style=ButtonStyle.PRIMARY,
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="✅ Check Membership",
            callback_data="check_membership",
            style=ButtonStyle.SUCCESS,
        )
    )
    return kb


def main_menu_keyboard(is_admin: bool = False) -> InlineKeyboardBuilder:
    """The colourful home menu described in the spec."""
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="🔍 Search Music",
            callback_data="search",
            style=ButtonStyle.PRIMARY,
        ),
        InlineKeyboardButton(
            text="📤 Upload Music",
            callback_data="upload",
            style=ButtonStyle.SUCCESS,
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="🔥 Top Downloads",
            callback_data="top_downloads",
            style=ButtonStyle.DANGER,
        ),
        InlineKeyboardButton(
            text="🎵 Categories",
            callback_data="categories",
            style=ButtonStyle.PRIMARY,
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="❤️ Favorites",
            callback_data="favorites",
            style=ButtonStyle.SUCCESS,
        ),
        InlineKeyboardButton(
            text="📜 My Uploads",
            callback_data="my_uploads",
            style=ButtonStyle.PRIMARY,
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="🎲 Random Music",
            callback_data="random",
            style=ButtonStyle.DANGER,
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="ℹ About",
            callback_data="about",
            style=ButtonStyle.SECONDARY,
        )
    )
    if is_admin:
        kb.row(
            InlineKeyboardButton(
                text="👑 Admin Panel",
                callback_data="admin_panel",
                style=ButtonStyle.PRIMARY,
            )
        )
    return kb


def search_submenu_keyboard() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="🎵 Search By Song Name",
            callback_data="search_by_name",
            style=ButtonStyle.PRIMARY,
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="🎤 Search By Artist",
            callback_data="search_by_artist",
            style=ButtonStyle.SUCCESS,
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="🏷 Search By Category",
            callback_data="search_by_category",
            style=ButtonStyle.PRIMARY,
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="⬅ Back",
            callback_data="back_home",
            style=ButtonStyle.SECONDARY,
        )
    )
    return kb


def song_action_keyboard(song_id: int, is_favorite: bool) -> InlineKeyboardBuilder:
    """Inline buttons under every song: ❤️ / ⬇ Download."""
    kb = InlineKeyboardBuilder()
    if is_favorite:
        kb.row(
            InlineKeyboardButton(
                text="💔 Remove Favorite",
                callback_data=f"remove_fav:{song_id}",
                style=ButtonStyle.DANGER,
            )
        )
    else:
        kb.row(
            InlineKeyboardButton(
                text="❤️ Favorite",
                callback_data=f"add_fav:{song_id}",
                style=ButtonStyle.SUCCESS,
            )
        )
    kb.row(
        InlineKeyboardButton(
            text="⬇ Download",
            callback_data=f"download:{song_id}",
            style=ButtonStyle.PRIMARY,
        )
    )
    return kb


def _pack_categories(
    cats: List[str], callback_prefix: str, back_to: str
) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for i in range(0, len(cats), 2):
        row = []
        for j in range(2):
            if i + j < len(cats):
                row.append(
                    InlineKeyboardButton(
                        text=f"🏷 {cats[i + j]}",
                        callback_data=f"{callback_prefix}:{cats[i + j]}",
                        style=ButtonStyle.PRIMARY,
                    )
                )
        if row:
            kb.row(*row)
    kb.row(
        InlineKeyboardButton(
            text="⬅ Back",
            callback_data=back_to,
            style=ButtonStyle.SECONDARY,
        )
    )
    return kb


async def build_categories_keyboard(
    callback_prefix: str, back_to: str = "back_home"
) -> InlineKeyboardBuilder:
    """Async wrapper used by handlers — fetches categories, packs them."""
    cats = await get_categories()
    return _pack_categories(cats, callback_prefix, back_to)


def admin_panel_keyboard() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="📊 Statistics",
            callback_data="admin_stats",
            style=ButtonStyle.PRIMARY,
        ),
        InlineKeyboardButton(
            text="👥 Users",
            callback_data="admin_users",
            style=ButtonStyle.SUCCESS,
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="🎵 Songs",
            callback_data="admin_songs",
            style=ButtonStyle.PRIMARY,
        ),
        InlineKeyboardButton(
            text="🗑 Delete Song",
            callback_data="admin_delete_song",
            style=ButtonStyle.DANGER,
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="📢 Broadcast",
            callback_data="admin_broadcast",
            style=ButtonStyle.SUCCESS,
        ),
        InlineKeyboardButton(
            text="🚫 Ban User",
            callback_data="admin_ban",
            style=ButtonStyle.DANGER,
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="✅ Unban User",
            callback_data="admin_unban",
            style=ButtonStyle.SUCCESS,
        ),
        InlineKeyboardButton(
            text="➕ Add Category",
            callback_data="admin_add_category",
            style=ButtonStyle.PRIMARY,
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="➖ Delete Category",
            callback_data="admin_delete_category",
            style=ButtonStyle.DANGER,
        ),
        InlineKeyboardButton(
            text="⚙ Settings",
            callback_data="admin_settings",
            style=ButtonStyle.SECONDARY,
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="⬅ Back",
            callback_data="back_home",
            style=ButtonStyle.SECONDARY,
        )
    )
    return kb


def back_keyboard(callback: str = "back_home") -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="⬅ Back",
            callback_data=callback,
            style=ButtonStyle.SECONDARY,
        )
    )
    return kb


# ═══════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def _format_date(iso: Optional[str]) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).strftime("%Y/%m/%d %H:%M")
    except Exception:
        return iso


async def check_membership(bot: Bot, user_id: int) -> bool:
    """Return True if the user is in the force-join channel."""
    if not FORCE_CHANNEL or is_admin(user_id):
        return True
    channel = _normalize_channel(FORCE_CHANNEL)
    if not channel:
        return True
    try:
        if channel.lstrip("-").isdigit():
            chat_id: Any = int(channel)
        else:
            chat_id = f"@{channel}"
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning("Membership check failed (%s) — allowing through.", e)
        # If we can't check, don't lock the user out.
        return True


async def _send_song(bot: Bot, chat_id: int, song, user_id: int) -> None:
    """Send a single song with full info and inline buttons."""
    try:
        if not song or not song["audio_file_id"]:
            await bot.send_message(chat_id, "❌ فایل صوتی یافت نشد.")
            return

        fav = await is_favorite(user_id, song["id"])
        caption = (
            f"🎵 <b>{song['song_name']}</b>\n"
            f"🎤 <b>Artist:</b> {song['artist'] or 'Unknown'}\n"
            f"🏷 <b>Category:</b> {song['category'] or 'Uncategorized'}\n"
            f"📤 <b>Uploader:</b> <code>{song['uploader_id']}</code>\n"
            f"⬇ <b>Downloads:</b> {song['downloads']}\n"
            f"📅 <b>Date:</b> {_format_date(song['upload_date'])}"
        )
        await bot.send_audio(
            chat_id=chat_id,
            audio=song["audio_file_id"],
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=song_action_keyboard(song["id"], fav).as_markup(),
        )
        await increment_views(song["id"])
    except Exception as e:
        logger.error("send_song error for id=%s: %s", song["id"] if song else "?", e)
        try:
            await bot.send_message(chat_id, f"❌ خطا در ارسال آهنگ: {e}")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
#  ROUTER & HANDLERS
# ═══════════════════════════════════════════════════════════════════
router = Router(name="main")


# ── /start ──────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, state: FSMContext):
    try:
        await state.clear()
        user = message.from_user
        if not user:
            return

        if await is_banned(user.id):
            await message.answer("🚫 شما از ربات مسدود شده‌اید.")
            return

        await add_user(user.id, user.username or "", user.first_name or "")

        if not await check_membership(bot, user.id):
            await message.answer(
                "👋 <b>خوش آمدید!</b>\n\n"
                "🌟 برای استفاده از ربات، لطفاً ابتدا در کانال ما عضو شوید.\n\n"
                "👇 روی دکمه زیر کلیک کنید:",
                reply_markup=force_join_keyboard().as_markup(),
                parse_mode=ParseMode.HTML,
            )
            return

        await message.answer(
            f"🎵 <b>به ربات موزیک خوش آمدید، "
            f"{user.first_name or 'دوست عزیز'}!</b>\n\n"
            "✨ از منوی زیر گزینه مورد نظر را انتخاب کنید:",
            reply_markup=main_menu_keyboard(is_admin(user.id)).as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("cmd_start error: %s", e)
        try:
            await message.answer("❌ خطایی رخ داد. لطفاً دوباره تلاش کنید.")
        except Exception:
            pass


# ── check_membership / back_home ────────────────────────────────────
@router.callback_query(F.data == "check_membership")
async def cb_check_membership(call: CallbackQuery, bot: Bot, state: FSMContext):
    try:
        user = call.from_user
        if not user:
            return
        if await check_membership(bot, user.id):
            await call.message.edit_text(
                f"🎵 <b>خوش آمدید، {user.first_name or 'دوست عزیز'}!</b>\n\n"
                "✨ از منوی زیر گزینه مورد نظر را انتخاب کنید:",
                reply_markup=main_menu_keyboard(is_admin(user.id)).as_markup(),
                parse_mode=ParseMode.HTML,
            )
        else:
            await call.answer("❌ شما هنوز عضو کانال نشده‌اید!", show_alert=True)
    except Exception as e:
        logger.error("check_membership error: %s", e)
        try:
            await call.answer("❌ خطایی رخ داد.", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "back_home")
async def cb_back_home(call: CallbackQuery, state: FSMContext):
    try:
        await state.clear()
        user = call.from_user
        if not user:
            return
        await call.message.edit_text(
            "🎵 <b>منوی اصلی</b>\n\n"
            "✨ از منوی زیر گزینه مورد نظر را انتخاب کنید:",
            reply_markup=main_menu_keyboard(is_admin(user.id)).as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("back_home error: %s", e)
        try:
            await call.answer("❌ خطایی رخ داد.", show_alert=True)
        except Exception:
            pass


# ── about ───────────────────────────────────────────────────────────
@router.callback_query(F.data == "about")
async def cb_about(call: CallbackQuery):
    try:
        text = (
            "ℹ <b>درباره ربات</b>\n\n"
            "🤖 <b>ربات موزیک تلگرام</b>\n"
            "📌 نسخه: 1.0.0\n"
            "💎 ساخته شده با aiogram 3.29 + Python 3.12\n"
            "🎧 جستجو، آپلود و اشتراک‌گذاری موزیک\n\n"
            "🌟 برای استفاده از امکانات، از منوی اصلی گزینه‌ای را انتخاب کنید."
        )
        await call.message.edit_text(
            text,
            reply_markup=back_keyboard().as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("about error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


# ═══════════════════════════════════════════════════════════════════
#  SEARCH
# ═══════════════════════════════════════════════════════════════════
@router.callback_query(F.data == "search")
async def cb_search(call: CallbackQuery):
    try:
        await call.message.edit_text(
            "🔍 <b>جستجوی موزیک</b>\n\n"
            "✨ یکی از روش‌های زیر را انتخاب کنید:",
            reply_markup=search_submenu_keyboard().as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("search menu error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.callback_query(F.data == "search_by_name")
async def cb_search_by_name(call: CallbackQuery, state: FSMContext):
    try:
        await state.set_state(SearchStates.query)
        await state.update_data(search_type="name")
        await call.message.edit_text(
            "🎵 <b>جستجو بر اساس نام آهنگ</b>\n\n"
            "✍️ نام آهنگ مورد نظر را وارد کنید:",
            reply_markup=back_keyboard("search").as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("search_by_name error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.callback_query(F.data == "search_by_artist")
async def cb_search_by_artist(call: CallbackQuery, state: FSMContext):
    try:
        await state.set_state(SearchStates.query)
        await state.update_data(search_type="artist")
        await call.message.edit_text(
            "🎤 <b>جستجو بر اساس خواننده</b>\n\n"
            "✍️ نام خواننده را وارد کنید:",
            reply_markup=back_keyboard("search").as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("search_by_artist error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.callback_query(F.data == "search_by_category")
async def cb_search_by_category(call: CallbackQuery):
    try:
        kb = await build_categories_keyboard("scat", "search")
        await call.message.edit_text(
            "🏷 <b>جستجو بر اساس دسته‌بندی</b>\n\n"
            "✨ یکی از دسته‌ها را انتخاب کنید:",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("search_by_category error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.callback_query(F.data.startswith("scat:"))
async def cb_scat(call: CallbackQuery):
    try:
        cat = call.data.split(":", 1)[1]
        results = await search_by_category(cat)
        if not results:
            await call.answer("❌ نتیجه‌ای یافت نشد.", show_alert=True)
            return
        await call.message.edit_text(
            f"🏷 <b>دسته:</b> {cat}\n"
            f"📊 <b>تعداد نتایج:</b> {len(results)}",
            parse_mode=ParseMode.HTML,
        )
        for song in results:
            await _send_song(call.bot, call.message.chat.id,
                             song, call.from_user.id)
        await call.message.answer(
            "✅ همه نتایج ارسال شد.",
            reply_markup=back_keyboard("back_home").as_markup(),
        )
    except Exception as e:
        logger.error("scat error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.message(SearchStates.query)
async def msg_search_query(message: Message, state: FSMContext):
    try:
        if not message.text:
            await message.answer("❌ لطفاً یک عبارت جستجو وارد کنید.")
            return
        data = await state.get_data()
        search_type = data.get("search_type", "name")
        query = message.text.strip()
        await state.clear()

        if not query:
            await message.answer("❌ عبارت جستجو نمی‌تواند خالی باشد.")
            return

        if search_type == "artist":
            results = await search_by_artist(query)
        else:
            results = await search_songs(query)

        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="🔍 Search Again",
                callback_data="search",
                style=ButtonStyle.PRIMARY,
            ),
            InlineKeyboardButton(
                text="⬅ Home",
                callback_data="back_home",
                style=ButtonStyle.SECONDARY,
            ),
        )

        if not results:
            await message.answer(
                f"❌ <b>نتیجه‌ای برای «{query}» یافت نشد.</b>",
                reply_markup=kb.as_markup(),
                parse_mode=ParseMode.HTML,
            )
            return

        await message.answer(
            f"🔍 <b>نتایج جستجو برای «{query}»:</b>\n"
            f"📊 <b>تعداد:</b> {len(results)}",
            parse_mode=ParseMode.HTML,
        )
        for song in results:
            await _send_song(message.bot, message.chat.id,
                             song, message.from_user.id)
        await message.answer("✅ همه نتایج ارسال شد.",
                             reply_markup=kb.as_markup())
    except Exception as e:
        logger.error("search_query error: %s", e)
        await state.clear()
        try:
            await message.answer("❌ خطایی رخ داد.")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
#  UPLOAD FSM (5 steps)
# ═══════════════════════════════════════════════════════════════════
@router.callback_query(F.data == "upload")
async def cb_upload(call: CallbackQuery, state: FSMContext):
    try:
        await state.set_state(UploadStates.song_name)
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="❌ Cancel",
                callback_data="back_home",
                style=ButtonStyle.DANGER,
            )
        )
        await call.message.edit_text(
            "📤 <b>آپلود موزیک — مرحله ۱/۵</b>\n\n"
            "🎵 <b>نام آهنگ را وارد کنید:</b>",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("upload start error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.message(UploadStates.song_name)
async def msg_upload_name(message: Message, state: FSMContext):
    try:
        if not message.text:
            await message.answer("❌ لطفاً نام آهنگ را به‌صورت متن وارد کنید.")
            return
        name = message.text.strip()
        if not name or len(name) > 200:
            await message.answer("❌ نام نامعتبر است (۱–۲۰۰ کاراکتر).")
            return
        await state.update_data(song_name=name)
        await state.set_state(UploadStates.artist)
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="⬅ Back",
                callback_data="upload",
                style=ButtonStyle.SECONDARY,
            )
        )
        await message.answer(
            f"📤 <b>آپلود موزیک — مرحله ۲/۵</b>\n\n"
            f"🎵 نام: <b>{name}</b>\n\n"
            "🎤 <b>نام خواننده را وارد کنید:</b>",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("upload name error: %s", e)
        await state.clear()


@router.message(UploadStates.artist)
async def msg_upload_artist(message: Message, state: FSMContext):
    try:
        if not message.text:
            await message.answer("❌ لطفاً نام خواننده را وارد کنید.")
            return
        artist = message.text.strip()
        if not artist or len(artist) > 200:
            await message.answer("❌ نام خواننده نامعتبر است.")
            return
        await state.update_data(artist=artist)
        await state.set_state(UploadStates.category)
        kb = await build_categories_keyboard("up_cat", "upload")
        data = await state.get_data()
        await message.answer(
            f"📤 <b>آپلود موزیک — مرحله ۳/۵</b>\n\n"
            f"🎵 نام: <b>{data.get('song_name')}</b>\n"
            f"🎤 خواننده: <b>{artist}</b>\n\n"
            "🏷 <b>دسته‌بندی را انتخاب کنید:</b>",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("upload artist error: %s", e)
        await state.clear()


@router.message(UploadStates.category)
async def msg_upload_category_invalid(message: Message, state: FSMContext):
    """If user types instead of clicking a category button."""
    try:
        await message.answer(
            "❌ لطفاً از بین دکمه‌ها یکی از دسته‌ها را انتخاب کنید."
        )
    except Exception:
        pass


@router.callback_query(
    F.data.startswith("up_cat:"),
    UploadStates.category,
)
async def cb_upload_cat(call: CallbackQuery, state: FSMContext):
    try:
        cat = call.data.split(":", 1)[1]
        await state.update_data(category=cat)
        await state.set_state(UploadStates.cover)
        data = await state.get_data()
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="⏭ Skip Cover",
                callback_data="skip_cover",
                style=ButtonStyle.SUCCESS,
            )
        )
        kb.row(
            InlineKeyboardButton(
                text="⬅ Back",
                callback_data="upload",
                style=ButtonStyle.SECONDARY,
            )
        )
        await call.message.edit_text(
            f"📤 <b>آپلود موزیک — مرحله ۴/۵</b>\n\n"
            f"🎵 نام: <b>{data.get('song_name')}</b>\n"
            f"🎤 خواننده: <b>{data.get('artist')}</b>\n"
            f"🏷 دسته: <b>{cat}</b>\n\n"
            "🖼 <b>تصویر کاور را ارسال کنید</b> (یا روی Skip بزنید):",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("upload cat error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.callback_query(F.data == "skip_cover")
async def cb_skip_cover(call: CallbackQuery, state: FSMContext):
    try:
        await state.update_data(cover_file_id=None)
        await state.set_state(UploadStates.audio)
        data = await state.get_data()
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="❌ Cancel",
                callback_data="back_home",
                style=ButtonStyle.DANGER,
            )
        )
        await call.message.edit_text(
            f"📤 <b>آپلود موزیک — مرحله ۵/۵</b>\n\n"
            f"🎵 نام: <b>{data.get('song_name')}</b>\n"
            f"🎤 خواننده: <b>{data.get('artist')}</b>\n"
            f"🏷 دسته: <b>{data.get('category')}</b>\n"
            f"🖼 کاور: <i>رد شد</i>\n\n"
            "🎧 <b>فایل صوتی را ارسال کنید (فقط Audio):</b>",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("skip_cover error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.message(UploadStates.cover, F.photo)
async def msg_upload_cover(message: Message, state: FSMContext):
    try:
        cover = message.photo[-1]
        await state.update_data(cover_file_id=cover.file_id)
        await state.set_state(UploadStates.audio)
        data = await state.get_data()
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="❌ Cancel",
                callback_data="back_home",
                style=ButtonStyle.DANGER,
            )
        )
        await message.answer(
            f"📤 <b>آپلود موزیک — مرحله ۵/۵</b>\n\n"
            f"🎵 نام: <b>{data.get('song_name')}</b>\n"
            f"🎤 خواننده: <b>{data.get('artist')}</b>\n"
            f"🏷 دسته: <b>{data.get('category')}</b>\n"
            f"🖼 کاور: ✅\n\n"
            "🎧 <b>فایل صوتی را ارسال کنید (فقط Audio):</b>",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("upload cover error: %s", e)
        await state.clear()


@router.message(UploadStates.cover)
async def msg_upload_cover_invalid(message: Message, state: FSMContext):
    try:
        await message.answer(
            "❌ لطفاً فقط تصویر ارسال کنید یا از دکمهٔ «⏭ Skip Cover» استفاده کنید."
        )
    except Exception:
        pass


@router.message(UploadStates.audio, F.audio)
async def msg_upload_audio(message: Message, state: FSMContext):
    try:
        audio = message.audio
        if not audio:
            await message.answer("❌ فایل صوتی نامعتبر است.")
            return
        data = await state.get_data()
        song_id = await add_song(
            song_name=data.get("song_name", "Unknown"),
            artist=data.get("artist", "Unknown"),
            category=data.get("category", "Other"),
            cover_file_id=data.get("cover_file_id"),
            audio_file_id=audio.file_id,
            uploader_id=message.from_user.id,
        )
        await state.clear()
        kb = InlineKeyboardBuilder()
        if song_id:
            kb.row(
                InlineKeyboardButton(
                    text="📤 Upload Another",
                    callback_data="upload",
                    style=ButtonStyle.SUCCESS,
                ),
                InlineKeyboardButton(
                    text="⬅ Home",
                    callback_data="back_home",
                    style=ButtonStyle.PRIMARY,
                ),
            )
            await message.answer(
                f"✅ <b>آهنگ با موفقیت آپلود شد!</b>\n\n"
                f"🆔 شناسه: <code>{song_id}</code>\n"
                f"🎵 نام: <b>{data.get('song_name')}</b>\n"
                f"🎤 خواننده: <b>{data.get('artist')}</b>\n"
                f"🏷 دسته: <b>{data.get('category')}</b>",
                reply_markup=kb.as_markup(),
                parse_mode=ParseMode.HTML,
            )
        else:
            kb.row(
                InlineKeyboardButton(
                    text="⬅ Home",
                    callback_data="back_home",
                    style=ButtonStyle.SECONDARY,
                )
            )
            await message.answer(
                "⚠️ <b>این فایل صوتی قبلاً در ربات آپلود شده است!</b>",
                reply_markup=kb.as_markup(),
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        logger.error("upload audio error: %s", e)
        try:
            await message.answer("❌ خطا در ذخیره‌سازی آهنگ.")
        except Exception:
            pass
        await state.clear()


@router.message(UploadStates.audio)
async def msg_upload_audio_invalid(message: Message, state: FSMContext):
    try:
        await message.answer(
            "❌ <b>فقط فایل صوتی (Audio) مجاز است!</b>\n\n"
            "ویدیو، سند، وویس و عکس پذیرفته نمی‌شوند.\n"
            "لطفاً فایل را به‌صورت Audio ارسال کنید.",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
#  FAVORITES
# ═══════════════════════════════════════════════════════════════════
@router.callback_query(F.data == "favorites")
async def cb_favorites(call: CallbackQuery):
    try:
        favs = await get_user_favorites(call.from_user.id)
        if not favs:
            await call.message.edit_text(
                "❤️ <b>علاقه‌مندی‌ها</b>\n\n"
                "📭 لیست علاقه‌مندی‌های شما خالی است.",
                reply_markup=back_keyboard().as_markup(),
                parse_mode=ParseMode.HTML,
            )
            return
        await call.message.edit_text(
            f"❤️ <b>علاقه‌مندی‌های شما</b>\n"
            f"📊 تعداد: {len(favs)}",
            parse_mode=ParseMode.HTML,
        )
        for song in favs:
            await _send_song(call.bot, call.message.chat.id,
                             song, call.from_user.id)
        await call.message.answer(
            "✅ همه آهنگ‌های مورد علاقه ارسال شد.",
            reply_markup=back_keyboard().as_markup(),
        )
    except Exception as e:
        logger.error("favorites error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.callback_query(F.data.startswith("add_fav:"))
async def cb_add_fav(call: CallbackQuery):
    try:
        song_id = int(call.data.split(":", 1)[1])
        await add_favorite(call.from_user.id, song_id)
        await call.answer("✅ به علاقه‌مندی‌ها اضافه شد!", show_alert=True)
    except Exception as e:
        logger.error("add_fav error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.callback_query(F.data.startswith("remove_fav:"))
async def cb_remove_fav(call: CallbackQuery):
    try:
        song_id = int(call.data.split(":", 1)[1])
        await remove_favorite(call.from_user.id, song_id)
        await call.answer("✅ از علاقه‌مندی‌ها حذف شد.", show_alert=True)
    except Exception as e:
        logger.error("remove_fav error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


# ═══════════════════════════════════════════════════════════════════
#  TOP DOWNLOADS  (top 20)
# ═══════════════════════════════════════════════════════════════════
@router.callback_query(F.data == "top_downloads")
async def cb_top_downloads(call: CallbackQuery):
    try:
        songs = await get_top_downloads(20)
        if not songs:
            await call.message.edit_text(
                "🔥 <b>پر دانلودترین‌ها</b>\n\n"
                "📭 هنوز آهنگی آپلود نشده است.",
                reply_markup=back_keyboard().as_markup(),
                parse_mode=ParseMode.HTML,
            )
            return
        await call.message.edit_text(
            f"🔥 <b>۲۰ آهنگ پر دانلود</b>\n"
            f"📊 تعداد: {len(songs)}",
            parse_mode=ParseMode.HTML,
        )
        for song in songs:
            await _send_song(call.bot, call.message.chat.id,
                             song, call.from_user.id)
        await call.message.answer(
            "✅ لیست پر دانلودترین‌ها ارسال شد.",
            reply_markup=back_keyboard().as_markup(),
        )
    except Exception as e:
        logger.error("top_downloads error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


# ═══════════════════════════════════════════════════════════════════
#  MY UPLOADS
# ═══════════════════════════════════════════════════════════════════
@router.callback_query(F.data == "my_uploads")
async def cb_my_uploads(call: CallbackQuery):
    try:
        uploads = await get_user_uploads(call.from_user.id)
        if not uploads:
            kb = InlineKeyboardBuilder()
            kb.row(
                InlineKeyboardButton(
                    text="📤 Upload",
                    callback_data="upload",
                    style=ButtonStyle.PRIMARY,
                ),
                InlineKeyboardButton(
                    text="⬅ Home",
                    callback_data="back_home",
                    style=ButtonStyle.SECONDARY,
                ),
            )
            await call.message.edit_text(
                "📜 <b>آپلودهای من</b>\n\n"
                "📭 شما هنوز آهنگی آپلود نکرده‌اید.",
                reply_markup=kb.as_markup(),
                parse_mode=ParseMode.HTML,
            )
            return
        kb = InlineKeyboardBuilder()
        for song in uploads[:50]:
            kb.row(
                InlineKeyboardButton(
                    text=f"🎵 {song['song_name']}  •  ⬇ {song['downloads']}",
                    callback_data=f"myu_view:{song['id']}",
                    style=ButtonStyle.PRIMARY,
                )
            )
        kb.row(
            InlineKeyboardButton(
                text="⬅ Home",
                callback_data="back_home",
                style=ButtonStyle.SECONDARY,
            )
        )
        await call.message.edit_text(
            f"📜 <b>آپلودهای من</b>\n"
            f"📊 تعداد: {len(uploads)}",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("my_uploads error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.callback_query(F.data.startswith("myu_view:"))
async def cb_myu_view(call: CallbackQuery):
    try:
        song_id = int(call.data.split(":", 1)[1])
        song = await get_song_by_id(song_id)
        if not song or song["uploader_id"] != call.from_user.id:
            await call.answer("❌ آهنگ یافت نشد.", show_alert=True)
            return
        await _send_song(call.bot, call.message.chat.id,
                         song, call.from_user.id)
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="🗑 Delete This Song",
                callback_data=f"myu_del:{song_id}",
                style=ButtonStyle.DANGER,
            )
        )
        kb.row(
            InlineKeyboardButton(
                text="⬅ Back",
                callback_data="my_uploads",
                style=ButtonStyle.SECONDARY,
            )
        )
        await call.message.answer("⚙️ <b>گزینه‌ها:</b>",
                                  reply_markup=kb.as_markup(),
                                  parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error("myu_view error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.callback_query(F.data.startswith("myu_del:"))
async def cb_myu_del(call: CallbackQuery):
    try:
        song_id = int(call.data.split(":", 1)[1])
        song = await get_song_by_id(song_id)
        if not song or song["uploader_id"] != call.from_user.id:
            await call.answer("❌ آهنگ یافت نشد.", show_alert=True)
            return
        await delete_song(song_id)
        await call.answer("✅ آهنگ حذف شد.", show_alert=True)
        await call.message.answer(
            f"✅ آهنگ «{song['song_name']}» حذف شد.",
            reply_markup=back_keyboard("my_uploads").as_markup(),
        )
    except Exception as e:
        logger.error("myu_del error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


# ═══════════════════════════════════════════════════════════════════
#  RANDOM
# ═══════════════════════════════════════════════════════════════════
@router.callback_query(F.data == "random")
async def cb_random(call: CallbackQuery):
    try:
        song = await get_random_song()
        if not song:
            await call.answer("❌ هیچ آهنگی برای ارسال وجود ندارد.",
                              show_alert=True)
            return
        await call.answer("🎲 در حال ارسال آهنگ تصادفی...")
        await _send_song(call.bot, call.message.chat.id,
                         song, call.from_user.id)
    except Exception as e:
        logger.error("random error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


# ═══════════════════════════════════════════════════════════════════
#  CATEGORIES (browse)
# ═══════════════════════════════════════════════════════════════════
@router.callback_query(F.data == "categories")
async def cb_categories(call: CallbackQuery):
    try:
        kb = await build_categories_keyboard("cat_view", "back_home")
        await call.message.edit_text(
            "🎵 <b>دسته‌بندی‌ها</b>\n\n"
            "✨ یکی از دسته‌ها را انتخاب کنید:",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("categories menu error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.callback_query(F.data.startswith("cat_view:"))
async def cb_cat_view(call: CallbackQuery):
    try:
        cat = call.data.split(":", 1)[1]
        results = await search_by_category(cat)
        if not results:
            await call.answer("❌ آهنگی در این دسته یافت نشد.",
                              show_alert=True)
            return
        await call.message.edit_text(
            f"🏷 <b>دسته:</b> {cat}\n"
            f"📊 تعداد: {len(results)}",
            parse_mode=ParseMode.HTML,
        )
        for song in results:
            await _send_song(call.bot, call.message.chat.id,
                             song, call.from_user.id)
        await call.message.answer(
            "✅ همه آهنگ‌های این دسته ارسال شد.",
            reply_markup=back_keyboard().as_markup(),
        )
    except Exception as e:
        logger.error("cat_view error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


# ═══════════════════════════════════════════════════════════════════
#  DOWNLOAD (inline button)
# ═══════════════════════════════════════════════════════════════════
@router.callback_query(F.data.startswith("download:"))
async def cb_download(call: CallbackQuery):
    try:
        song_id = int(call.data.split(":", 1)[1])
        song = await get_song_by_id(song_id)
        if not song:
            await call.answer("❌ آهنگ یافت نشد.", show_alert=True)
            return
        await increment_downloads(song_id, call.from_user.id)
        await call.bot.send_audio(
            chat_id=call.message.chat.id,
            audio=song["audio_file_id"],
            caption=f"⬇ <b>{song['song_name']}</b>\n✅ دانلود شد!",
            parse_mode=ParseMode.HTML,
        )
        await call.answer("✅ دانلود شد!", show_alert=True)
    except Exception as e:
        logger.error("download error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


# ═══════════════════════════════════════════════════════════════════
#  ADMIN PANEL
# ═══════════════════════════════════════════════════════════════════
@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(call: CallbackQuery):
    try:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        await call.message.edit_text(
            "👑 <b>پنل مدیریت</b>\n\n"
            "✨ یکی از گزینه‌ها را انتخاب کنید:",
            reply_markup=admin_panel_keyboard().as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("admin_panel error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


# ── statistics ──────────────────────────────────────────────────────
@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    try:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        users = await get_user_count()
        songs = await get_song_count()
        downloads = await get_download_count()
        today_users = await get_today_users()
        today_uploads = await get_today_uploads()
        text = (
            "📊 <b>آمار ربات</b>\n\n"
            f"👥 کل کاربران: <b>{users}</b>\n"
            f"🎵 کل آهنگ‌ها: <b>{songs}</b>\n"
            f"⬇ کل دانلودها: <b>{downloads}</b>\n"
            f"📅 کاربران امروز: <b>{today_users}</b>\n"
            f"📤 آپلودهای امروز: <b>{today_uploads}</b>"
        )
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="🔄 Refresh",
                callback_data="admin_stats",
                style=ButtonStyle.SUCCESS,
            )
        )
        kb.row(
            InlineKeyboardButton(
                text="⬅ Back",
                callback_data="admin_panel",
                style=ButtonStyle.SECONDARY,
            )
        )
        await call.message.edit_text(
            text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error("admin_stats error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


# ── users ───────────────────────────────────────────────────────────
@router.callback_query(F.data == "admin_users")
async def cb_admin_users(call: CallbackQuery):
    try:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        count = await get_user_count()
        await call.message.edit_text(
            f"👥 <b>کاربران</b>\n\n"
            f"📊 تعداد کل: <b>{count}</b> کاربر",
            reply_markup=back_keyboard("admin_panel").as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("admin_users error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


# ── songs ───────────────────────────────────────────────────────────
@router.callback_query(F.data == "admin_songs")
async def cb_admin_songs(call: CallbackQuery):
    try:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        count = await get_song_count()
        await call.message.edit_text(
            f"🎵 <b>آهنگ‌ها</b>\n\n"
            f"📊 تعداد کل: <b>{count}</b> آهنگ",
            reply_markup=back_keyboard("admin_panel").as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("admin_songs error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


# ── delete song (FSM) ───────────────────────────────────────────────
@router.callback_query(F.data == "admin_delete_song")
async def cb_admin_delete_song(call: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        await state.set_state(DeleteSongStates.song_id)
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="❌ Cancel",
                callback_data="admin_panel",
                style=ButtonStyle.DANGER,
            )
        )
        await call.message.edit_text(
            "🗑 <b>حذف آهنگ</b>\n\n"
            "🆔 شناسه آهنگ را وارد کنید:",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("admin_delete_song error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.message(DeleteSongStates.song_id)
async def msg_admin_delete_song(message: Message, state: FSMContext):
    try:
        if not is_admin(message.from_user.id):
            await state.clear()
            return
        if not message.text or not message.text.strip().lstrip("-").isdigit():
            await message.answer("❌ لطفاً یک شناسهٔ عددی معتبر وارد کنید.")
            return
        song_id = int(message.text.strip())
        song = await get_song_by_id(song_id)
        if not song:
            await message.answer(f"❌ آهنگی با شناسهٔ {song_id} یافت نشد.")
            return
        await delete_song(song_id)
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="🗑 Delete Another",
                callback_data="admin_delete_song",
                style=ButtonStyle.PRIMARY,
            ),
            InlineKeyboardButton(
                text="⬅ Admin Panel",
                callback_data="admin_panel",
                style=ButtonStyle.SECONDARY,
            ),
        )
        await message.answer(
            f"✅ آهنگ «{song['song_name']}» (ID: {song_id}) حذف شد.",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        logger.error("admin_delete_song finish error: %s", e)
        await state.clear()


# ── broadcast (FSM) ─────────────────────────────────────────────────
@router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        await state.set_state(BroadcastStates.text)
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="❌ Cancel",
                callback_data="admin_panel",
                style=ButtonStyle.DANGER,
            )
        )
        await call.message.edit_text(
            "📢 <b>ارسال همگانی (Broadcast)</b>\n\n"
            "✍️ پیام خود را وارد کنید (متن، عکس، یا هر نوع محتوایی):",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("admin_broadcast start error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.message(BroadcastStates.text)
async def msg_admin_broadcast(message: Message, state: FSMContext, bot: Bot):
    try:
        if not is_admin(message.from_user.id):
            await state.clear()
            return
        await state.clear()
        users = await get_all_users()
        success = 0
        failed = 0
        status_msg = await message.answer(
            f"📤 در حال ارسال همگانی به {len(users)} کاربر..."
        )
        for idx, uid in enumerate(users, start=1):
            try:
                await bot.copy_message(
                    chat_id=uid,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                success += 1
            except Exception as e:
                failed += 1
                logger.debug("Broadcast failed for %s: %s", uid, e)
            # Throttle to respect Telegram flood limits
            if idx % 25 == 0:
                await asyncio.sleep(1)
        summary = (
            f"✅ <b>ارسال همگانی تمام شد</b>\n\n"
            f"✅ موفق: <b>{success}</b>\n"
            f"❌ ناموفق: <b>{failed}</b>\n"
            f"📊 کل: <b>{len(users)}</b>"
        )
        try:
            await status_msg.edit_text(summary, parse_mode=ParseMode.HTML)
        except Exception:
            await message.answer(summary, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error("admin_broadcast error: %s", e)
        try:
            await message.answer("❌ خطا در ارسال همگانی.")
        except Exception:
            pass
        await state.clear()


# ── ban (FSM) ───────────────────────────────────────────────────────
@router.callback_query(F.data == "admin_ban")
async def cb_admin_ban(call: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        await state.set_state(BanStates.user_id)
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="⬅ Cancel",
                callback_data="admin_panel",
                style=ButtonStyle.SECONDARY,
            )
        )
        await call.message.edit_text(
            "🚫 <b>مسدود کردن کاربر</b>\n\n"
            "🆔 شناسهٔ کاربر را وارد کنید:",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("admin_ban error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.message(BanStates.user_id)
async def msg_admin_ban(message: Message, state: FSMContext):
    try:
        if not is_admin(message.from_user.id):
            await state.clear()
            return
        if not message.text or not message.text.strip().lstrip("-").isdigit():
            await message.answer("❌ لطفاً یک شناسهٔ عددی معتبر وارد کنید.")
            return
        user_id = int(message.text.strip())
        await add_ban(user_id)
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="🚫 Ban Another",
                callback_data="admin_ban",
                style=ButtonStyle.PRIMARY,
            ),
            InlineKeyboardButton(
                text="⬅ Admin Panel",
                callback_data="admin_panel",
                style=ButtonStyle.SECONDARY,
            ),
        )
        await message.answer(
            f"✅ کاربر <code>{user_id}</code> مسدود شد.",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        logger.error("admin_ban finish error: %s", e)
        await state.clear()


# ── unban (FSM) ─────────────────────────────────────────────────────
@router.callback_query(F.data == "admin_unban")
async def cb_admin_unban(call: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        await state.set_state(UnbanStates.user_id)
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="⬅ Cancel",
                callback_data="admin_panel",
                style=ButtonStyle.SECONDARY,
            )
        )
        await call.message.edit_text(
            "✅ <b>رفع مسدودیت کاربر</b>\n\n"
            "🆔 شناسهٔ کاربر را وارد کنید:",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("admin_unban error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.message(UnbanStates.user_id)
async def msg_admin_unban(message: Message, state: FSMContext):
    try:
        if not is_admin(message.from_user.id):
            await state.clear()
            return
        if not message.text or not message.text.strip().lstrip("-").isdigit():
            await message.answer("❌ لطفاً یک شناسهٔ عددی معتبر وارد کنید.")
            return
        user_id = int(message.text.strip())
        await remove_ban(user_id)
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="✅ Unban Another",
                callback_data="admin_unban",
                style=ButtonStyle.PRIMARY,
            ),
            InlineKeyboardButton(
                text="⬅ Admin Panel",
                callback_data="admin_panel",
                style=ButtonStyle.SECONDARY,
            ),
        )
        await message.answer(
            f"✅ مسدودیت کاربر <code>{user_id}</code> رفع شد.",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        logger.error("admin_unban finish error: %s", e)
        await state.clear()


# ── add category (FSM) ──────────────────────────────────────────────
@router.callback_query(F.data == "admin_add_category")
async def cb_admin_add_category(call: CallbackQuery, state: FSMContext):
    try:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        await state.set_state(AddCategoryStates.name)
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="⬅ Cancel",
                callback_data="admin_panel",
                style=ButtonStyle.SECONDARY,
            )
        )
        await call.message.edit_text(
            "➕ <b>افزودن دسته‌بندی</b>\n\n"
            "✍️ نام دسته را وارد کنید:",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("admin_add_category error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.message(AddCategoryStates.name)
async def msg_admin_add_category(message: Message, state: FSMContext):
    try:
        if not is_admin(message.from_user.id):
            await state.clear()
            return
        if not message.text:
            await message.answer("❌ لطفاً یک نام وارد کنید.")
            return
        name = message.text.strip()
        if not name or len(name) > 50:
            await message.answer("❌ نام نامعتبر است (۱–۵۰ کاراکتر).")
            return
        ok = await add_category(name)
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text="➕ Add Another",
                callback_data="admin_add_category",
                style=ButtonStyle.PRIMARY,
            ),
            InlineKeyboardButton(
                text="⬅ Admin Panel",
                callback_data="admin_panel",
                style=ButtonStyle.SECONDARY,
            ),
        )
        if ok:
            await message.answer(
                f"✅ دستهٔ «{name}» اضافه شد.",
                reply_markup=kb.as_markup(),
            )
        else:
            await message.answer(
                f"⚠️ دستهٔ «{name}» از قبل وجود دارد.",
                reply_markup=kb.as_markup(),
            )
    except Exception as e:
        logger.error("admin_add_category finish error: %s", e)
        await state.clear()


# ── delete category ─────────────────────────────────────────────────
@router.callback_query(F.data == "admin_delete_category")
async def cb_admin_delete_category(call: CallbackQuery):
    try:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        cats = await get_categories()
        if not cats:
            await call.answer("❌ دسته‌ای برای حذف وجود ندارد.",
                              show_alert=True)
            return
        kb = InlineKeyboardBuilder()
        for cat in cats:
            kb.row(
                InlineKeyboardButton(
                    text=f"🗑 {cat}",
                    callback_data=f"del_cat:{cat}",
                    style=ButtonStyle.DANGER,
                )
            )
        kb.row(
            InlineKeyboardButton(
                text="⬅ Back",
                callback_data="admin_panel",
                style=ButtonStyle.SECONDARY,
            )
        )
        await call.message.edit_text(
            "➖ <b>حذف دسته‌بندی</b>\n\n"
            "یکی از دسته‌ها را برای حذف انتخاب کنید:",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("admin_delete_category error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


@router.callback_query(F.data.startswith("del_cat:"))
async def cb_del_cat(call: CallbackQuery):
    try:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        cat = call.data.split(":", 1)[1]
        await delete_category(cat)
        await call.answer(f"✅ دستهٔ «{cat}» حذف شد.", show_alert=True)
        # Re-render the delete-category screen
        await cb_admin_delete_category(call)
    except Exception as e:
        logger.error("del_cat error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


# ── settings (read-only view) ───────────────────────────────────────
@router.callback_query(F.data == "admin_settings")
async def cb_admin_settings(call: CallbackQuery):
    try:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        await call.message.edit_text(
            "⚙ <b>تنظیمات</b>\n\n"
            f"📢 کانال اجباری: <code>{FORCE_CHANNEL or 'تنظیم نشده'}</code>\n"
            f"👑 ادمین: <code>{ADMIN_ID}</code>\n"
            f"💾 دیتابیس: <code>{DB_PATH}</code>\n"
            f"📊 نسخه: <b>1.0.0</b>",
            reply_markup=back_keyboard("admin_panel").as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("admin_settings error: %s", e)
        await call.answer("❌ خطایی رخ داد.", show_alert=True)


# ═══════════════════════════════════════════════════════════════════
#  GLOBAL ERROR HANDLER
# ═══════════════════════════════════════════════════════════════════
@router.error()
async def on_error(event):
    """Catch-all: never crash the bot on an unhandled update."""
    exc = getattr(event, "exception", None)
    if isinstance(exc, Exception):
        logger.error("Unhandled exception in handler: %s", exc,
                     exc_info=exc)
    else:
        logger.error("Error event without exception: %r", event)
    return True  # mark as handled so the dispatcher doesn't re-raise


# ═══════════════════════════════════════════════════════════════════
#  ENTRY-POINT
# ═══════════════════════════════════════════════════════════════════
async def main() -> None:
    logger.info("Initialising database...")
    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Bot is starting polling…")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logger.info("Bot session closed. Bye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot interrupted by user.")
    except Exception as e:
        logger.error("Fatal error: %s", e)
        logger.error(traceback.format_exc())
        sys.exit(1)
