#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Music Bot — Persian UI
Python 3.12 • aiogram 3.30 • SQLite (aiosqlite) • Railway-ready

Admin features: stats, users, songs, broadcast, ban/unban, button manager.
Songs can be searched by name/artist. The Button Manager lets the admin
change every button's text and colour from inside the bot (no redeploy).

Environment:
    BOT_TOKEN, ADMIN_ID, FORCE_CHANNEL (optional), DATA_DIR, DB_FILE
"""

import asyncio
import html
import logging
import os
import sys
from datetime import datetime
from typing import Any, Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ButtonStyle, ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ─── Config ──────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "")
_DATA_DIR = os.getenv("DATA_DIR", "/data")
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
    DB_PATH = os.path.join(_DATA_DIR, os.getenv("DB_FILE", "music.db"))
except OSError:
    DB_PATH = os.path.join(os.getcwd(), os.getenv("DB_FILE", "music.db"))

# ─── Button system ───────────────────────────────────────────────────
# Each button has a unique key. Default text & colour are defined here;
# admins can override them at runtime via the Button Manager (stored in
# DB and mirrored in the in-memory cache for sync keyboard building).
BUTTON_DEFS: dict[str, tuple[str, str]] = {
    # Main menu
    "search":         ("🔍 Search Music",     ButtonStyle.PRIMARY),
    "upload":         ("📤 Upload Music",     ButtonStyle.SUCCESS),
    "top_downloads":  ("🔥 Top Downloads",    ButtonStyle.DANGER),
    "favorites":      ("❤️ Favorites",         ButtonStyle.SUCCESS),
    "my_uploads":     ("📜 My Uploads",       ButtonStyle.PRIMARY),
    "random":         ("🎲 Random Music",     ButtonStyle.DANGER),
    "about":          ("ℹ About",             ButtonStyle.PRIMARY),
    "admin_panel":    ("👑 Admin Panel",      ButtonStyle.PRIMARY),
    # Force join
    "join_channel":   ("📢 Join Channel",      ButtonStyle.PRIMARY),
    "check_member":   ("✅ Check Membership",  ButtonStyle.SUCCESS),
    # Search
    "search_name":    ("🎵 By Song Name",     ButtonStyle.PRIMARY),
    "search_artist":  ("🎤 By Artist",        ButtonStyle.SUCCESS),
    "search_again":   ("🔍 Search Again",     ButtonStyle.PRIMARY),
    # Song actions
    "fav_add":        ("❤️ Favorite",         ButtonStyle.SUCCESS),
    "fav_remove":     ("💔 Remove Favorite",  ButtonStyle.DANGER),
    "download":       ("⬇ Download",          ButtonStyle.PRIMARY),
    # My uploads
    "myu_delete":     ("🗑 Delete This Song", ButtonStyle.DANGER),
    "myu_back":       ("📜 My Uploads",       ButtonStyle.PRIMARY),
    # Admin
    "adm_stats":      ("📊 Statistics",       ButtonStyle.PRIMARY),
    "adm_users":      ("👥 Users",            ButtonStyle.SUCCESS),
    "adm_songs":      ("🎵 Songs",            ButtonStyle.PRIMARY),
    "adm_delete_song":("🗑 Delete Song",      ButtonStyle.DANGER),
    "adm_broadcast":  ("📢 Broadcast",        ButtonStyle.SUCCESS),
    "adm_ban":        ("🚫 Ban User",         ButtonStyle.DANGER),
    "adm_unban":      ("✅ Unban User",       ButtonStyle.SUCCESS),
    "adm_buttons":    ("🎨 Button Manager",   ButtonStyle.PRIMARY),
    # Admin actions
    "adm_ban_again":  ("🚫 Ban Another",      ButtonStyle.PRIMARY),
    "adm_unban_again":("✅ Unban Another",     ButtonStyle.PRIMARY),
    "adm_del_again":  ("🗑 Delete Another",   ButtonStyle.PRIMARY),
    "adm_refresh":    ("🔄 Refresh",          ButtonStyle.SUCCESS),
    # Upload FSM
    "up_cancel":      ("❌ Cancel",           ButtonStyle.DANGER),
    "up_skip_cover":  ("⏭ Skip Cover",        ButtonStyle.SUCCESS),
    "up_back":        ("⬅ Back",             ButtonStyle.PRIMARY),
    "up_another":     ("📤 Upload Another",   ButtonStyle.SUCCESS),
    # Generic
    "back":           ("⬅ Back",             ButtonStyle.PRIMARY),
    "home":           ("🏠 Home",             ButtonStyle.PRIMARY),
    "cancel":         ("❌ Cancel",           ButtonStyle.DANGER),
    "skip":           ("⏭ Skip",             ButtonStyle.SUCCESS),
    "delete":         ("🗑 Delete",           ButtonStyle.DANGER),
    "refresh":        ("🔄 Refresh",          ButtonStyle.SUCCESS),
    "search_back":    ("🔍 Search",          ButtonStyle.PRIMARY),
}

BUTTON_GROUPS: dict[str, list[str]] = {
    "Main Menu":    ["search", "upload", "top_downloads", "favorites",
                     "my_uploads", "random", "about", "admin_panel"],
    "Force Join":   ["join_channel", "check_member"],
    "Search":       ["search_name", "search_artist", "search_again", "search_back"],
    "Song Actions": ["fav_add", "fav_remove", "download"],
    "My Uploads":   ["myu_delete", "myu_back"],
    "Admin Panel":  ["adm_stats", "adm_users", "adm_songs", "adm_delete_song",
                     "adm_broadcast", "adm_ban", "adm_unban", "adm_buttons"],
    "Admin Actions": ["adm_ban_again", "adm_unban_again", "adm_del_again", "adm_refresh"],
    "Upload":       ["up_cancel", "up_skip_cover", "up_back", "up_another"],
    "Generic":      ["back", "home", "cancel", "skip", "delete", "refresh"],
}

# In-memory cache: key -> (text, style). Reloaded on startup and on every
# admin override. Keeps keyboard builders synchronous.
_BTN_CACHE: dict[str, tuple[str, str]] = {}


def btn(key: str, **kw) -> InlineKeyboardButton:
    """Build a button from a registered key. Text/colour come from the
    cache (with fallback to defaults) so admin overrides take effect
    without a restart."""
    text, style = _BTN_CACHE.get(key, BUTTON_DEFS[key])
    return InlineKeyboardButton(text=text, style=style, **kw)


def _find_group(key: str) -> str:
    for group, keys in BUTTON_GROUPS.items():
        if key in keys:
            return group
    return "Other"


# ─── Logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
for noisy in ("aiogram.event", "aiogram.dispatcher"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("bot")

# ─── FSM States ──────────────────────────────────────────────────────
class UploadStates(StatesGroup):
    song_name = State()
    artist = State()
    cover = State()
    audio = State()


class BroadcastStates(StatesGroup):
    text = State()


class DeleteSongStates(StatesGroup):
    song_id = State()


class BanStates(StatesGroup):
    user_id = State()


class UnbanStates(StatesGroup):
    user_id = State()


class SearchStates(StatesGroup):
    query = State()


class EditButtonStates(StatesGroup):
    text = State()


# ─── Database ────────────────────────────────────────────────────────
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        join_date TEXT)""",
    """CREATE TABLE IF NOT EXISTS songs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        song_name TEXT NOT NULL, artist TEXT,
        cover_file_id TEXT, audio_file_id TEXT UNIQUE,
        uploader_id INTEGER, downloads INTEGER DEFAULT 0,
        views INTEGER DEFAULT 0, upload_date TEXT)""",
    """CREATE TABLE IF NOT EXISTS favorites (
        user_id INTEGER, song_id INTEGER, added_date TEXT,
        PRIMARY KEY (user_id, song_id))""",
    """CREATE TABLE IF NOT EXISTS downloads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, song_id INTEGER, download_date TEXT)""",
    """CREATE TABLE IF NOT EXISTS banned (
        user_id INTEGER PRIMARY KEY, ban_date TEXT, reason TEXT)""",
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT)""",
    """CREATE TABLE IF NOT EXISTS button_settings (
        key TEXT PRIMARY KEY, text TEXT NOT NULL, style TEXT NOT NULL)""",
]


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        for stmt in SCHEMA:
            await db.execute(stmt)
        # Cleanup from older versions of the bot
        await db.execute("DROP TABLE IF EXISTS categories")
        await db.commit()
    await load_button_cache()
    logger.info("Database initialised at %s", DB_PATH)


# ─── User helpers ────────────────────────────────────────────────────
async def add_user(user_id: int, username: str, first_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, join_date) "
            "VALUES (?,?,?,?)",
            (user_id, username, first_name, datetime.now().isoformat()),
        )
        await db.commit()


async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM banned WHERE user_id=?", (user_id,))
        return (await cur.fetchone()) is not None


async def add_ban(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO banned (user_id, ban_date) VALUES (?,?)",
            (user_id, datetime.now().isoformat()),
        )
        await db.commit()


async def remove_ban(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM banned WHERE user_id=?", (user_id,))
        await db.commit()


async def get_user_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        return (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]


async def get_today_users() -> int:
    today = datetime.now().date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        return (await (await db.execute(
            "SELECT COUNT(*) FROM users WHERE join_date LIKE ?", (f"{today}%",)
        )).fetchone())[0]


async def get_all_users() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        return [r[0] for r in await (await db.execute(
            "SELECT user_id FROM users")).fetchall()]


# ─── Song helpers ────────────────────────────────────────────────────
async def add_song(name, artist, cover, audio, uploader) -> Optional[int]:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO songs (song_name, artist, cover_file_id, "
                "audio_file_id, uploader_id, upload_date) VALUES (?,?,?,?,?,?)",
                (name, artist, cover, audio, uploader, datetime.now().isoformat()),
            )
            await db.commit()
            return cur.lastrowid
    except aiosqlite.IntegrityError:
        return None


async def get_song(song_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute(
            "SELECT * FROM songs WHERE id=?", (song_id,))).fetchone()


async def search_songs(query: str, by: str = "name", limit: int = 50):
    col = "song_name" if by == "name" else "artist"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute(
            f"SELECT * FROM songs WHERE LOWER({col}) LIKE ? "
            f"COLLATE NOCASE ORDER BY downloads DESC LIMIT ?",
            (f"%{query.lower()}%", limit),
        )).fetchall()


async def increment_downloads(song_id: int, user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE songs SET downloads = downloads + 1 WHERE id=?", (song_id,)
        )
        await db.execute(
            "INSERT INTO downloads (user_id, song_id, download_date) VALUES (?,?,?)",
            (user_id, song_id, datetime.now().isoformat()),
        )
        await db.commit()


async def increment_views(song_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE songs SET views = views + 1 WHERE id=?", (song_id,))
        await db.commit()


async def get_top_downloads(limit: int = 20):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute(
            "SELECT * FROM songs ORDER BY downloads DESC LIMIT ?", (limit,)
        )).fetchall()


async def get_random_song():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute(
            "SELECT * FROM songs ORDER BY RANDOM() LIMIT 1")).fetchone()


async def get_user_uploads(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute(
            "SELECT * FROM songs WHERE uploader_id=? ORDER BY upload_date DESC",
            (user_id,),
        )).fetchall()


async def delete_song(song_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM songs WHERE id=?", (song_id,))
        await db.execute("DELETE FROM favorites WHERE song_id=?", (song_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_song_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        return (await (await db.execute("SELECT COUNT(*) FROM songs")).fetchone())[0]


async def get_download_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        return (await (await db.execute("SELECT COUNT(*) FROM downloads")).fetchone())[0]


async def get_today_uploads() -> int:
    today = datetime.now().date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        return (await (await db.execute(
            "SELECT COUNT(*) FROM songs WHERE upload_date LIKE ?", (f"{today}%",)
        )).fetchone())[0]


# ─── Favorites helpers ───────────────────────────────────────────────
async def add_favorite(user_id: int, song_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO favorites (user_id, song_id, added_date) "
            "VALUES (?,?,?)",
            (user_id, song_id, datetime.now().isoformat()),
        )
        await db.commit()


async def remove_favorite(user_id: int, song_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM favorites WHERE user_id=? AND song_id=?",
            (user_id, song_id),
        )
        await db.commit()


async def get_user_favorites(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute(
            "SELECT s.* FROM songs s JOIN favorites f ON s.id=f.song_id "
            "WHERE f.user_id=? ORDER BY f.added_date DESC", (user_id,),
        )).fetchall()


async def is_favorite(user_id: int, song_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM favorites WHERE user_id=? AND song_id=?",
            (user_id, song_id),
        )
        return (await cur.fetchone()) is not None


# ─── Button settings ─────────────────────────────────────────────────
async def load_button_cache() -> None:
    """Reload admin overrides from DB into the in-memory cache."""
    _BTN_CACHE.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT key, text, style FROM button_settings")
        for key, text, style in await cur.fetchall():
            _BTN_CACHE[key] = (text, style)


async def set_button(key: str, text: Optional[str] = None,
                     style: Optional[str] = None) -> None:
    """Persist an override. Falls back to current value if not given."""
    cur_text, cur_style = _BTN_CACHE.get(key, BUTTON_DEFS[key])
    new_text = text if text is not None else cur_text
    new_style = style if style is not None else cur_style
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO button_settings (key, text, style) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET text=excluded.text, style=excluded.style",
            (key, new_text, new_style),
        )
        await db.commit()
    _BTN_CACHE[key] = (new_text, new_style)


# ─── Generic helpers ─────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def esc(value) -> str:
    """HTML-escape user-controlled text for safe message rendering."""
    return html.escape(str(value), quote=False) if value is not None else ""


def _normalize_channel(channel: str) -> str:
    channel = (channel or "").strip()
    for prefix in ("https://t.me/", "t.me/"):
        if channel.startswith(prefix):
            channel = channel.split(prefix, 1)[1]
    return channel.lstrip("@")


async def check_membership(bot: Bot, user_id: int) -> bool:
    if not FORCE_CHANNEL or is_admin(user_id):
        return True
    channel = _normalize_channel(FORCE_CHANNEL)
    if not channel:
        return True
    try:
        chat_id: Any = (int(channel) if channel.lstrip("-").isdigit()
                        else f"@{channel}")
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning("Membership check failed: %s — allowing through", e)
        return True


def _format_date(iso) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).strftime("%Y/%m/%d %H:%M")
    except Exception:
        return iso


async def _send_song(bot: Bot, chat_id: int, song, user_id: int) -> None:
    """Send a single song (with cover, info, action buttons)."""
    if not song or not song["audio_file_id"]:
        await bot.send_message(chat_id, "❌ فایل صوتی یافت نشد.")
        return
    fav = await is_favorite(user_id, song["id"])
    caption = (
        f"🎵 <b>{song['song_name']}</b>\n"
        f"🎤 <b>Artist:</b> {song['artist'] or 'Unknown'}\n"
        f"📤 <b>Uploader:</b> <code>{song['uploader_id']}</code>\n"
        f"⬇ <b>Downloads:</b> {song['downloads']}\n"
        f"📅 <b>Date:</b> {_format_date(song['upload_date'])}"
    )
    await bot.send_audio(
        chat_id=chat_id, audio=song["audio_file_id"],
        caption=caption, parse_mode=ParseMode.HTML,
        reply_markup=song_action_keyboard(song["id"], fav).as_markup(),
    )
    await increment_views(song["id"])


# ─── Keyboards ───────────────────────────────────────────────────────
def force_join_keyboard() -> InlineKeyboardBuilder:
    url = f"https://t.me/{_normalize_channel(FORCE_CHANNEL)}" if FORCE_CHANNEL else "https://t.me/"
    kb = InlineKeyboardBuilder()
    kb.row(btn("join_channel", url=url))
    kb.row(btn("check_member", callback_data="check_membership"))
    return kb


def main_menu_keyboard(is_admin: bool = False) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(btn("search", callback_data="search"),
           btn("upload", callback_data="upload"))
    kb.row(btn("top_downloads", callback_data="top_downloads"),
           btn("favorites", callback_data="favorites"))
    kb.row(btn("my_uploads", callback_data="my_uploads"),
           btn("random", callback_data="random"))
    kb.row(btn("about", callback_data="about"))
    if is_admin:
        kb.row(btn("admin_panel", callback_data="admin_panel"))
    return kb


def search_submenu_keyboard() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(btn("search_name", callback_data="search_by_name"))
    kb.row(btn("search_artist", callback_data="search_by_artist"))
    kb.row(btn("back", callback_data="back_home"))
    return kb


def song_action_keyboard(song_id: int, is_fav: bool) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    key = "fav_remove" if is_fav else "fav_add"
    action = "remove_fav" if is_fav else "add_fav"
    kb.row(btn(key, callback_data=f"{action}:{song_id}"))
    kb.row(btn("download", callback_data=f"download:{song_id}"))
    return kb


def admin_panel_keyboard() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(btn("adm_stats", callback_data="admin_stats"),
           btn("adm_users", callback_data="admin_users"))
    kb.row(btn("adm_songs", callback_data="admin_songs"),
           btn("adm_delete_song", callback_data="admin_delete_song"))
    kb.row(btn("adm_broadcast", callback_data="admin_broadcast"),
           btn("adm_ban", callback_data="admin_ban"))
    kb.row(btn("adm_unban", callback_data="admin_unban"),
           btn("adm_buttons", callback_data="admin_buttons"))
    kb.row(btn("back", callback_data="back_home"))
    return kb


def back_keyboard(callback: str = "back_home") -> InlineKeyboardBuilder:
    return InlineKeyboardBuilder().row(btn("back", callback_data=callback))


def cancel_keyboard(callback: str = "back_home") -> InlineKeyboardBuilder:
    return InlineKeyboardBuilder().row(btn("cancel", callback_data=callback))


def my_uploads_item_keyboard(song_id: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(btn("myu_delete", callback_data=f"myu_del:{song_id}"))
    kb.row(btn("myu_back", callback_data="my_uploads"))
    return kb


# ─── Router & global error handler ───────────────────────────────────
router = Router(name="main")


@router.error()
async def on_error(event):
    """Single point of failure: log + notify the user. Keeps the
    bot running even when one handler blows up."""
    exc = getattr(event, "exception", None)
    if isinstance(exc, Exception):
        logger.exception("Unhandled error", exc_info=exc)
    update = getattr(event, "update", None)
    if update:
        try:
            if update.callback_query:
                await update.callback_query.answer("❌ خطایی رخ داد.", show_alert=True)
            elif update.message:
                await update.message.answer("❌ خطایی رخ داد.")
        except Exception:
            pass
    return True


# ─── Start / about / back-home ───────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, state: FSMContext):
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
            "🌟 برای استفاده از ربات، لطفاً ابتدا در کانال ما عضو شوید.",
            reply_markup=force_join_keyboard().as_markup(),
            parse_mode=ParseMode.HTML,
        )
        return
    await message.answer(
        f"🎵 <b>به ربات موزیک خوش آمدید، {esc(user.first_name) or 'دوست عزیز'}!</b>\n\n"
        "✨ از منوی زیر گزینه مورد نظر را انتخاب کنید:",
        reply_markup=main_menu_keyboard(is_admin(user.id)).as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "check_membership")
async def cb_check_membership(call: CallbackQuery, bot: Bot, state: FSMContext):
    user = call.from_user
    if await check_membership(bot, user.id):
        await call.message.edit_text(
            f"🎵 <b>خوش آمدید، {esc(user.first_name) or 'دوست عزیز'}!</b>\n\n"
            "✨ از منوی زیر گزینه مورد نظر را انتخاب کنید:",
            reply_markup=main_menu_keyboard(is_admin(user.id)).as_markup(),
            parse_mode=ParseMode.HTML,
        )
    else:
        await call.answer("❌ شما هنوز عضو کانال نشده‌اید!", show_alert=True)


@router.callback_query(F.data == "back_home")
async def cb_back_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "🎵 <b>منوی اصلی</b>\n\n✨ از منوی زیر گزینه مورد نظر را انتخاب کنید:",
        reply_markup=main_menu_keyboard(is_admin(call.from_user.id)).as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "about")
async def cb_about(call: CallbackQuery):
    await call.message.edit_text(
        "ℹ <b>درباره ربات</b>\n\n"
        "🤖 ربات موزیک تلگرام\n"
        "💎 aiogram 3.30 + Python 3.12\n"
        "🎧 جستجو، آپلود و اشتراک‌گذاری موزیک",
        reply_markup=back_keyboard().as_markup(),
        parse_mode=ParseMode.HTML,
    )


# ─── Search ──────────────────────────────────────────────────────────
@router.callback_query(F.data == "search")
async def cb_search(call: CallbackQuery):
    await call.message.edit_text(
        "🔍 <b>جستجوی موزیک</b>\n\nیک روش رو انتخاب کن:",
        reply_markup=search_submenu_keyboard().as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "search_by_name")
async def cb_search_by_name(call: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.query)
    await state.update_data(search_type="name")
    await call.message.edit_text(
        "🎵 <b>جستجو بر اساس نام</b>\n\nنام آهنگ رو بفرست:",
        reply_markup=back_keyboard("search").as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "search_by_artist")
async def cb_search_by_artist(call: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.query)
    await state.update_data(search_type="artist")
    await call.message.edit_text(
        "🎤 <b>جستجو بر اساس خواننده</b>\n\nنام خواننده رو بفرست:",
        reply_markup=back_keyboard("search").as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.message(SearchStates.query)
async def msg_search_query(message: Message, state: FSMContext):
    if not message.text:
        return
    data = await state.get_data()
    query = message.text.strip()
    await state.clear()
    if not query:
        await message.answer("❌ عبارت جستجو نمی‌تواند خالی باشد.")
        return
    results = await (search_songs(query, "artist")
                     if data.get("search_type") == "artist"
                     else search_songs(query, "name"))
    kb = InlineKeyboardBuilder()
    if not results:
        kb.row(btn("search_again", callback_data="search"),
               btn("home", callback_data="back_home"))
        await message.answer(
            f"❌ <b>نتیجه‌ای برای «{esc(query)}» یافت نشد.</b>",
            reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML,
        )
        return
    await message.answer(
        f"🔍 <b>نتایج برای «{esc(query)}»:</b> {len(results)} مورد",
        parse_mode=ParseMode.HTML,
    )
    for song in results:
        await _send_song(message.bot, message.chat.id, song, message.from_user.id)
    await message.answer("✅ همه نتایج ارسال شد.",
                         reply_markup=back_keyboard("back_home").as_markup())


# ─── Upload FSM ──────────────────────────────────────────────────────
@router.callback_query(F.data == "upload")
async def cb_upload(call: CallbackQuery, state: FSMContext):
    await state.set_state(UploadStates.song_name)
    await call.message.edit_text(
        "📤 <b>آپلود موزیک — مرحله ۱/۴</b>\n\n🎵 نام آهنگ رو بفرست:",
        reply_markup=cancel_keyboard().as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.message(UploadStates.song_name)
async def msg_upload_name(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ لطفاً نام آهنگ رو به‌صورت متن بفرست.")
        return
    name = message.text.strip()
    if not 1 <= len(name) <= 200:
        await message.answer("❌ نام نامعتبر است (۱–۲۰۰ کاراکتر).")
        return
    await state.update_data(song_name=name)
    await state.set_state(UploadStates.artist)
    await message.answer(
        f"📤 <b>مرحله ۲/۴</b>\n\n🎵 نام: <b>{esc(name)}</b>\n\n"
        "🎤 نام خواننده رو بفرست:",
        reply_markup=back_keyboard("upload").as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.message(UploadStates.artist)
async def msg_upload_artist(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ لطفاً نام خواننده رو بفرست.")
        return
    artist = message.text.strip()
    if not 1 <= len(artist) <= 200:
        await message.answer("❌ نام خواننده نامعتبر است.")
        return
    await state.update_data(artist=artist)
    await state.set_state(UploadStates.cover)
    data = await state.get_data()
    kb = InlineKeyboardBuilder()
    kb.row(btn("up_skip_cover", callback_data="skip_cover"))
    kb.row(btn("up_back", callback_data="upload"))
    await message.answer(
        f"📤 <b>مرحله ۳/۴</b>\n\n"
        f"🎵 نام: <b>{esc(data.get('song_name'))}</b>\n"
        f"🎤 خواننده: <b>{esc(artist)}</b>\n\n"
        "🖼 تصویر کاور رو بفرست (یا ⏭ Skip بزن):",
        reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "skip_cover", UploadStates.cover)
async def cb_skip_cover(call: CallbackQuery, state: FSMContext):
    await state.update_data(cover_file_id=None)
    await state.set_state(UploadStates.audio)
    data = await state.get_data()
    await call.message.edit_text(
        f"📤 <b>مرحله ۴/۴</b>\n\n"
        f"🎵 نام: <b>{esc(data.get('song_name'))}</b>\n"
        f"🎤 خواننده: <b>{esc(data.get('artist'))}</b>\n"
        f"🖼 کاور: <i>رد شد</i>\n\n"
        "🎧 فایل صوتی رو بفرست:",
        reply_markup=cancel_keyboard().as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.message(UploadStates.cover, F.photo)
async def msg_upload_cover(message: Message, state: FSMContext):
    cover = message.photo[-1]
    await state.update_data(cover_file_id=cover.file_id)
    await state.set_state(UploadStates.audio)
    data = await state.get_data()
    await message.answer(
        f"📤 <b>مرحله ۴/۴</b>\n\n"
        f"🎵 نام: <b>{esc(data.get('song_name'))}</b>\n"
        f"🎤 خواننده: <b>{esc(data.get('artist'))}</b>\n"
        f"🖼 کاور: ✅\n\n"
        "🎧 فایل صوتی رو بفرست:",
        reply_markup=cancel_keyboard().as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.message(UploadStates.cover)
async def msg_upload_cover_invalid(message: Message, state: FSMContext):
    await message.answer(
        "❌ لطفاً فقط تصویر بفرست یا از دکمهٔ «⏭ Skip Cover» استفاده کن."
    )


@router.message(UploadStates.audio, F.audio)
async def msg_upload_audio(message: Message, state: FSMContext):
    audio = message.audio
    if not audio:
        await message.answer("❌ فایل صوتی نامعتبر است.")
        return
    data = await state.get_data()
    song_id = await add_song(
        name=data.get("song_name", "Unknown"),
        artist=data.get("artist", "Unknown"),
        cover=data.get("cover_file_id"),
        audio=audio.file_id,
        uploader=message.from_user.id,
    )
    await state.clear()
    kb = InlineKeyboardBuilder()
    if song_id:
        kb.row(btn("up_another", callback_data="upload"),
               btn("home", callback_data="back_home"))
        await message.answer(
            f"✅ <b>آهنگ آپلود شد!</b>\n\n"
            f"🆔 <code>{song_id}</code>  🎵 {esc(data.get('song_name'))}\n"
            f"🎤 {esc(data.get('artist'))}",
            reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML,
        )
    else:
        kb.row(btn("home", callback_data="back_home"))
        await message.answer(
            "⚠️ <b>این فایل صوتی قبلاً آپلود شده!</b>",
            reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML,
        )


@router.message(UploadStates.audio)
async def msg_upload_audio_invalid(message: Message, state: FSMContext):
    await message.answer(
        "❌ <b>فقط فایل صوتی (Audio) مجاز است.</b>\n"
        "لطفاً فایل رو به‌صورت Audio بفرست.",
        parse_mode=ParseMode.HTML,
    )


# ─── Favorites ───────────────────────────────────────────────────────
@router.callback_query(F.data == "favorites")
async def cb_favorites(call: CallbackQuery):
    favs = await get_user_favorites(call.from_user.id)
    if not favs:
        await call.message.edit_text(
            "❤️ <b>علاقه‌مندی‌ها</b>\n\n📭 لیست علاقه‌مندی‌های شما خالی است.",
            reply_markup=back_keyboard().as_markup(),
            parse_mode=ParseMode.HTML,
        )
        return
    await call.message.edit_text(
        f"❤️ <b>علاقه‌مندی‌های شما</b>  ({len(favs)})",
        parse_mode=ParseMode.HTML,
    )
    for song in favs:
        await _send_song(call.bot, call.message.chat.id, song, call.from_user.id)
    await call.message.answer("✅ آهنگ‌ها ارسال شد.",
                              reply_markup=back_keyboard().as_markup())


@router.callback_query(F.data.startswith("add_fav:"))
async def cb_add_fav(call: CallbackQuery):
    await add_favorite(call.from_user.id, int(call.data.split(":", 1)[1]))
    await call.answer("✅ به علاقه‌مندی‌ها اضافه شد!", show_alert=True)


@router.callback_query(F.data.startswith("remove_fav:"))
async def cb_remove_fav(call: CallbackQuery):
    await remove_favorite(call.from_user.id, int(call.data.split(":", 1)[1]))
    await call.answer("✅ از علاقه‌مندی‌ها حذف شد.", show_alert=True)


# ─── Top downloads / random ──────────────────────────────────────────
@router.callback_query(F.data == "top_downloads")
async def cb_top_downloads(call: CallbackQuery):
    songs = await get_top_downloads(20)
    if not songs:
        await call.message.edit_text(
            "🔥 <b>پر دانلودترین‌ها</b>\n\n📭 هنوز آهنگی آپلود نشده.",
            reply_markup=back_keyboard().as_markup(),
            parse_mode=ParseMode.HTML,
        )
        return
    await call.message.edit_text(
        f"🔥 <b>۲۰ آهنگ پر دانلود</b>  ({len(songs)})",
        parse_mode=ParseMode.HTML,
    )
    for song in songs:
        await _send_song(call.bot, call.message.chat.id, song, call.from_user.id)
    await call.message.answer("✅ لیست ارسال شد.",
                              reply_markup=back_keyboard().as_markup())


@router.callback_query(F.data == "random")
async def cb_random(call: CallbackQuery):
    song = await get_random_song()
    if not song:
        await call.answer("❌ هیچ آهنگی برای ارسال وجود ندارد.", show_alert=True)
        return
    await call.answer("🎲 در حال ارسال...")
    await _send_song(call.bot, call.message.chat.id, song, call.from_user.id)


# ─── My uploads ──────────────────────────────────────────────────────
@router.callback_query(F.data == "my_uploads")
async def cb_my_uploads(call: CallbackQuery):
    uploads = await get_user_uploads(call.from_user.id)
    if not uploads:
        kb = InlineKeyboardBuilder()
        kb.row(btn("upload", callback_data="upload"),
               btn("home", callback_data="back_home"))
        await call.message.edit_text(
            "📜 <b>آپلودهای من</b>\n\n📭 هنوز آهنگی آپلود نکرده‌اید.",
            reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML,
        )
        return
    kb = InlineKeyboardBuilder()
    for song in uploads[:50]:
        kb.row(InlineKeyboardButton(
            text=f"🎵 {song['song_name']}  •  ⬇ {song['downloads']}",
            callback_data=f"myu_view:{song['id']}",
            style=ButtonStyle.PRIMARY,
        ))
    kb.row(btn("home", callback_data="back_home"))
    await call.message.edit_text(
        f"📜 <b>آپلودهای من</b>  ({len(uploads)})",
        reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("myu_view:"))
async def cb_myu_view(call: CallbackQuery):
    song_id = int(call.data.split(":", 1)[1])
    song = await get_song(song_id)
    if not song or song["uploader_id"] != call.from_user.id:
        await call.answer("❌ آهنگ یافت نشد.", show_alert=True)
        return
    await _send_song(call.bot, call.message.chat.id, song, call.from_user.id)
    await call.message.answer("⚙️ <b>گزینه‌ها:</b>",
                              reply_markup=my_uploads_item_keyboard(song_id).as_markup(),
                              parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("myu_del:"))
async def cb_myu_del(call: CallbackQuery):
    song_id = int(call.data.split(":", 1)[1])
    song = await get_song(song_id)
    if not song or song["uploader_id"] != call.from_user.id:
        await call.answer("❌ آهنگ یافت نشد.", show_alert=True)
        return
    await delete_song(song_id)
    await call.answer("✅ آهنگ حذف شد.", show_alert=True)
    await call.message.answer(
        f"✅ «{esc(song['song_name'])}» حذف شد.",
        reply_markup=back_keyboard("my_uploads").as_markup(),
        parse_mode=ParseMode.HTML,
    )


# ─── Download ────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("download:"))
async def cb_download(call: CallbackQuery):
    song_id = int(call.data.split(":", 1)[1])
    song = await get_song(song_id)
    if not song:
        await call.answer("❌ آهنگ یافت نشد.", show_alert=True)
        return
    await increment_downloads(song_id, call.from_user.id)
    await call.bot.send_audio(
        chat_id=call.message.chat.id, audio=song["audio_file_id"],
        caption=f"⬇ <b>{esc(song['song_name'])}</b>\n✅ دانلود شد!",
        parse_mode=ParseMode.HTML,
    )
    await call.answer("✅ دانلود شد!", show_alert=True)


# ─── Admin: panel ────────────────────────────────────────────────────
@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await call.message.edit_text(
        "👑 <b>پنل مدیریت</b>\n\nیک گزینه رو انتخاب کن:",
        reply_markup=admin_panel_keyboard().as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    users, songs, downloads = (await get_user_count(), await get_song_count(),
                               await get_download_count())
    today_u, today_s = await get_today_users(), await get_today_uploads()
    kb = InlineKeyboardBuilder()
    kb.row(btn("adm_refresh", callback_data="admin_stats"))
    kb.row(btn("back", callback_data="admin_panel"))
    await call.message.edit_text(
        f"📊 <b>آمار ربات</b>\n\n"
        f"👥 کاربران: <b>{users}</b>  (امروز: {today_u})\n"
        f"🎵 آهنگ‌ها: <b>{songs}</b>  (امروز: {today_s})\n"
        f"⬇ دانلودها: <b>{downloads}</b>",
        reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.in_({"admin_users", "admin_songs"}))
async def cb_admin_counts(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    if call.data == "admin_users":
        n = await get_user_count()
        title, emoji = "کاربران", "👥"
    else:
        n = await get_song_count()
        title, emoji = "آهنگ‌ها", "🎵"
    await call.message.edit_text(
        f"{emoji} <b>{title}</b>\n\n📊 تعداد کل: <b>{n}</b>",
        reply_markup=back_keyboard("admin_panel").as_markup(),
        parse_mode=ParseMode.HTML,
    )


# ─── Admin: delete song ──────────────────────────────────────────────
@router.callback_query(F.data == "admin_delete_song")
async def cb_admin_delete_song(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    await state.set_state(DeleteSongStates.song_id)
    await call.message.edit_text(
        "🗑 <b>حذف آهنگ</b>\n\n🆔 شناسه آهنگ رو بفرست:",
        reply_markup=cancel_keyboard("admin_panel").as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.message(DeleteSongStates.song_id)
async def msg_admin_delete_song(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    if not message.text or not message.text.strip().lstrip("-").isdigit():
        await message.answer("❌ لطفاً یک شناسهٔ عددی معتبر بفرست.")
        return
    song_id = int(message.text.strip())
    song = await get_song(song_id)
    if not song:
        await message.answer(f"❌ آهنگی با شناسهٔ {song_id} یافت نشد.")
        return
    await delete_song(song_id)
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(btn("adm_del_again", callback_data="admin_delete_song"),
           btn("back", callback_data="admin_panel"))
    await message.answer(
        f"✅ «{esc(song['song_name'])}» (ID: {song_id}) حذف شد.",
        reply_markup=kb.as_markup(),
    )


# ─── Admin: broadcast ────────────────────────────────────────────────
@router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    await state.set_state(BroadcastStates.text)
    await call.message.edit_text(
        "📢 <b>ارسال همگانی</b>\n\nپیامت رو بفرست:",
        reply_markup=cancel_keyboard("admin_panel").as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.message(BroadcastStates.text)
async def msg_admin_broadcast(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    await state.clear()
    users = await get_all_users()
    status = await message.answer(f"📤 در حال ارسال به {len(users)} کاربر...")
    success = failed = 0
    for idx, uid in enumerate(users, 1):
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id,
                                   message_id=message.message_id)
            success += 1
        except Exception as e:
            failed += 1
            logger.debug("Broadcast to %s failed: %s", uid, e)
        if idx % 25 == 0:
            await asyncio.sleep(1)
    summary = (f"✅ <b>ارسال همگانی تمام شد</b>\n\n"
               f"✅ موفق: <b>{success}</b>  ❌ ناموفق: <b>{failed}</b>")
    try:
        await status.edit_text(summary, parse_mode=ParseMode.HTML)
    except Exception:
        await message.answer(summary, parse_mode=ParseMode.HTML)


# ─── Admin: ban / unban ──────────────────────────────────────────────
async def _ban_fsm_start(call: CallbackQuery, state: FSMContext, target_state):
    if not is_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    await state.set_state(target_state)
    await call.message.edit_text(
        "🆔 شناسهٔ کاربر رو بفرست:",
        reply_markup=back_keyboard("admin_panel").as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "admin_ban")
async def cb_admin_ban(call: CallbackQuery, state: FSMContext):
    await _ban_fsm_start(call, state, BanStates.user_id)


@router.callback_query(F.data == "admin_unban")
async def cb_admin_unban(call: CallbackQuery, state: FSMContext):
    await _ban_fsm_start(call, state, UnbanStates.user_id)


@router.message(BanStates.user_id)
async def msg_admin_ban(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id) or not message.text:
        await state.clear()
        return
    if not message.text.strip().lstrip("-").isdigit():
        await message.answer("❌ لطفاً یک شناسهٔ عددی معتبر بفرست.")
        return
    await add_ban(int(message.text.strip()))
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(btn("adm_ban_again", callback_data="admin_ban"),
           btn("back", callback_data="admin_panel"))
    await message.answer("✅ کاربر مسدود شد.", reply_markup=kb.as_markup())


@router.message(UnbanStates.user_id)
async def msg_admin_unban(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id) or not message.text:
        await state.clear()
        return
    if not message.text.strip().lstrip("-").isdigit():
        await message.answer("❌ لطفاً یک شناسهٔ عددی معتبر بفرست.")
        return
    await remove_ban(int(message.text.strip()))
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(btn("adm_unban_again", callback_data="admin_unban"),
           btn("back", callback_data="admin_panel"))
    await message.answer("✅ مسدودیت کاربر رفع شد.", reply_markup=kb.as_markup())


# ─── Admin: Button Manager ───────────────────────────────────────────
@router.callback_query(F.data == "admin_buttons")
async def cb_admin_buttons(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for group in BUTTON_GROUPS:
        kb.row(InlineKeyboardButton(
            text=f"📁 {group}", callback_data=f"btn_grp:{group}",
            style=ButtonStyle.PRIMARY,
        ))
    kb.row(btn("back", callback_data="admin_panel"))
    await call.message.edit_text(
        "🎨 <b>مدیریت دکمه‌ها</b>\n\nیک گروه رو انتخاب کن:",
        reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("btn_grp:"))
async def cb_button_group(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    group = call.data.split(":", 1)[1]
    keys = BUTTON_GROUPS.get(group, [])
    style_emoji = {"primary": "🔵", "success": "🟢", "danger": "🔴"}
    kb = InlineKeyboardBuilder()
    for k in keys:
        text, style = _BTN_CACHE.get(k, BUTTON_DEFS[k])
        kb.row(InlineKeyboardButton(
            text=f"{style_emoji.get(style, '⚪')} {text}",
            callback_data=f"btn_edit:{k}",
            style=ButtonStyle.PRIMARY,
        ))
    kb.row(btn("back", callback_data="admin_buttons"))
    await call.message.edit_text(
        f"📁 <b>{group}</b>\n\nروی دکمه‌ای که می‌خوای ویرایش کنی بزن:",
        reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("btn_edit:"))
async def cb_button_edit(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    key = call.data.split(":", 1)[1]
    text, style = _BTN_CACHE.get(key, BUTTON_DEFS[key])
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="✏ تغییر متن", callback_data=f"btn_text:{key}",
        style=ButtonStyle.PRIMARY,
    ))
    kb.row(InlineKeyboardButton(
        text=f"🎨 تغییر رنگ  ({style})", callback_data=f"btn_color:{key}",
        style=ButtonStyle.SUCCESS,
    ))
    kb.row(btn("back", callback_data=f"btn_grp:{_find_group(key)}"))
    await call.message.edit_text(
        f"🎨 <b>ویرایش دکمه</b>\n\n"
        f"🔑 کلید: <code>{key}</code>\n"
        f"📝 متن فعلی: <b>{esc(text)}</b>\n"
        f"🎨 رنگ فعلی: <b>{style}</b>",
        reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("btn_color:"))
async def cb_button_color(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    key = call.data.split(":", 1)[1]
    text, _ = _BTN_CACHE.get(key, BUTTON_DEFS[key])
    kb = InlineKeyboardBuilder()
    for s in ("primary", "success", "danger"):
        emoji = {"primary": "🔵", "success": "🟢", "danger": "🔴"}[s]
        kb.row(InlineKeyboardButton(
            text=f"{emoji} {s.title()}", callback_data=f"btn_set:{key}:{s}",
            style=ButtonStyle(s),
        ))
    kb.row(btn("back", callback_data=f"btn_edit:{key}"))
    await call.message.edit_text(
        f"🎨 رنگ جدید برای <b>{esc(text)}</b>:",
        reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("btn_set:"))
async def cb_button_set(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    _, key, style = call.data.split(":", 2)
    if style not in ("primary", "success", "danger"):
        await call.answer("❌ رنگ نامعتبر", show_alert=True)
        return
    await set_button(key, style=style)
    await call.answer(f"✅ رنگ به {style} تغییر کرد", show_alert=True)
    # Re-render the edit menu with the new colour
    await cb_button_edit(call)


@router.callback_query(F.data.startswith("btn_text:"))
async def cb_button_text(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    key = call.data.split(":", 1)[1]
    text, _ = _BTN_CACHE.get(key, BUTTON_DEFS[key])
    await state.set_state(EditButtonStates.text)
    await state.update_data(edit_key=key)
    await call.message.edit_text(
        f"✏ <b>تغییر متن</b>\n\n"
        f"📝 متن فعلی: <b>{esc(text)}</b>\n\n"
        "متن جدید رو بفرست (۱–۱۰۰ کاراکتر):",
        reply_markup=back_keyboard(f"btn_edit:{key}").as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.message(EditButtonStates.text)
async def msg_edit_button_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id) or not message.text:
        await state.clear()
        return
    new_text = message.text.strip()
    if not 1 <= len(new_text) <= 100:
        await message.answer("❌ متن باید ۱–۱۰۰ کاراکتر باشه.")
        return
    data = await state.get_data()
    key = data.get("edit_key")
    await state.clear()
    if not key:
        return
    await set_button(key, text=new_text)
    await message.answer(
        f"✅ متن دکمه به <b>«{esc(new_text)}»</b> تغییر کرد.",
        reply_markup=back_keyboard(f"btn_edit:{key}").as_markup(),
        parse_mode=ParseMode.HTML,
    )


# ─── Healthcheck (for Railway) ───────────────────────────────────────
async def _start_healthcheck(port: int):
    try:
        from aiohttp import web
    except ImportError:
        logger.warning("aiohttp missing — healthcheck disabled")
        return None

    async def health(_):
        return web.Response(text="OK")

    app = web.Application()
    for path in ("/", "/health", "/healthz"):
        app.router.add_get(path, health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    try:
        await site.start()
        logger.info("Healthcheck listening on 0.0.0.0:%s", port)
    except OSError as e:
        logger.warning("Could not bind healthcheck port %s: %s", port, e)
        await runner.cleanup()
        return None
    return runner


# ─── Entry point ─────────────────────────────────────────────────────
async def main() -> None:
    if not BOT_TOKEN:
        print("FATAL: BOT_TOKEN environment variable is required.", file=sys.stderr)
        sys.exit(1)
    if ADMIN_ID == 0:
        print("WARNING: ADMIN_ID not set — admin panel will be inaccessible.",
              file=sys.stderr)

    print(f"  aiogram: {__import__('aiogram').__version__}")
    print(f"  DB:      {DB_PATH}")

    await init_db()

    bot = Bot(token=BOT_TOKEN,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    health = await _start_healthcheck(int(os.getenv("PORT", "8080")))

    logger.info("Bot is starting polling…")
    try:
        await dp.start_polling(bot)
    finally:
        if health is not None:
            try:
                await health.cleanup()
            except Exception:
                pass
        await bot.session.close()
        logger.info("Bot session closed. Bye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot interrupted.")
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
        sys.exit(1)
