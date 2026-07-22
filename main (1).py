"""
Telegram forwarder bot — colored "ارسال کد" button.

Robust version:
  - Explicit aiohttp session (avoids default-session lockups on long-running
    instances on Railway/Render/Fly)
  - Centralised error handler — one bad message never kills the polling loop
  - Bounded admin_msg map (no memory leak after thousands of messages)
  - All handlers wrapped in try/except so transient Telegram errors don't
    take the bot down
  - handle_signals=True → graceful shutdown on Railway's SIGTERM
  - Each user message also resets their own slot on /start (defensive)

Tested with aiogram 3.30.0 / Python 3.11+ / Bot API 9.4.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import suppress

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # fine on Railway — env vars come from the dashboard

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
    sys.stderr.write("❌ BOT_TOKEN is missing. Set it in env or .env file.\n")
    sys.exit(1)
if ADMIN_ID == 0:
    sys.stderr.write("❌ ADMIN_ID is missing. Set it in env or .env file.\n")
    sys.exit(1)

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Bot / Dispatcher
# --------------------------------------------------------------------------- #

# Explicit session with a generous timeout — the default aiohttp session can
# lock up after long idle periods on PaaS platforms.
session = AiohttpSession(timeout=60)

bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

# Explicit memory storage keeps the dispatcher state explicit
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# --------------------------------------------------------------------------- #
# Bounded map: admin's chat message id  →  original user id
# --------------------------------------------------------------------------- #

MAX_MAP_SIZE = 1000
user_by_admin_msg: dict[int, int] = {}


def remember_admin_msg(msg_id: int, user_id: int) -> None:
    """Add to map, evicting the oldest entry if the map is full."""
    if len(user_by_admin_msg) >= MAX_MAP_SIZE:
        oldest = next(iter(user_by_admin_msg))
        user_by_admin_msg.pop(oldest, None)
    user_by_admin_msg[msg_id] = user_id


# --------------------------------------------------------------------------- #
# States
# --------------------------------------------------------------------------- #


class UserStates(StatesGroup):
    waiting_for_code = State()


# --------------------------------------------------------------------------- #
# Keyboards
# --------------------------------------------------------------------------- #


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="ارسال کد",
                    callback_data="send_code",
                    style="primary",  # 🔵 blue — Bot API 9.4
                )
            ]
        ]
    )


# --------------------------------------------------------------------------- #
# Error handler — catches anything handlers throw so the polling loop survives
# --------------------------------------------------------------------------- #


@dp.error()
async def on_error(event_update, exception: Exception) -> bool:
    """aiogram error hook. Returning True marks the exception as handled."""
    logger.exception(
        "Handler crashed on update %s: %s",
        type(event_update).__name__,
        exception,
    )
    return True


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    try:
        await state.clear()
        await message.answer(
            "سلام! 👋\n"
            "برای ارسال کد روی دکمه‌ی زیر بزن:",
            reply_markup=main_keyboard(),
        )
    except Exception:
        logger.exception("cmd_start failed")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    try:
        await state.clear()
        await message.answer("لغو شد. هر وقت خواستی دوباره /start بزن.")
    except Exception:
        logger.exception("cmd_cancel failed")


@router.callback_query(F.data == "send_code")
async def on_send_code(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        await state.set_state(UserStates.waiting_for_code)
        if callback.message:
            try:
                await callback.message.edit_text("بفرست کدت رو 👇")
            except Exception:
                # Message too old / already edited → fall back to a new one
                with suppress(Exception):
                    await callback.message.answer("بفرست کدت رو 👇")
        # Always answer the callback so the loading spinner goes away
        with suppress(Exception):
            await callback.answer()
    except Exception:
        logger.exception("on_send_code failed")
        with suppress(Exception):
            await callback.answer("خطا", show_alert=True)


@router.message(UserStates.waiting_for_code)
async def receive_code(message: Message, state: FSMContext) -> None:
    try:
        user = message.from_user
        if user is None:
            return

        # 1) Info header to admin
        info = (
            "📩 پیام جدید:\n"
            f"👤 نام: {user.full_name}\n"
            f"🆔 آیدی: <code>{user.id}</code>\n"
            f"🔗 یوزرنیم: @{user.username or 'ندارد'}\n"
            "─────────────────"
        )
        info_msg = await bot.send_message(ADMIN_ID, info)
        remember_admin_msg(info_msg.message_id, user.id)

        # 2) Forward the actual message (text/media)
        sent_msg_id: int | None = None
        try:
            fwd = await bot.forward_message(
                chat_id=ADMIN_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            sent_msg_id = fwd.message_id
        except Exception as exc:
            logger.warning("forward_message failed (%s) → copy_message", exc)
            try:
                cp = await bot.copy_message(
                    chat_id=ADMIN_ID,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                sent_msg_id = cp.message_id
            except Exception as exc2:
                logger.warning("copy_message failed (%s) → text fallback", exc2)
                if message.text:
                    sent = await bot.send_message(
                        ADMIN_ID, f"💬 {message.text}"
                    )
                    sent_msg_id = sent.message_id

        if sent_msg_id is not None:
            remember_admin_msg(sent_msg_id, user.id)

        await message.answer("✅ کدت ارسال شد! منتظر جواب باشم.")
        await state.clear()
    except Exception:
        logger.exception("receive_code failed")
        with suppress(Exception):
            await message.answer("❌ خطایی رخ داد. دوباره /start بزن.")
        with suppress(Exception):
            await state.clear()


@router.message(F.reply_to_message)
async def admin_reply(message: Message) -> None:
    try:
        if message.from_user is None or message.from_user.id != ADMIN_ID:
            return

        user_id = user_by_admin_msg.get(message.reply_to_message.message_id)
        if user_id is None:
            return

        if message.text:
            await bot.send_message(
                user_id, f"💬 پاسخ ادمین:\n\n{message.text}"
            )
        else:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=ADMIN_ID,
                message_id=message.message_id,
            )
        await message.reply("✅ پاسخ ارسال شد.")
    except Exception:
        logger.exception("admin_reply failed")
        with suppress(Exception):
            await message.reply("❌ خطا در ارسال پاسخ.")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def main() -> None:
    # Clean any stale webhook so polling is the only active source
    with suppress(Exception):
        await bot.delete_webhook(drop_pending_updates=True)

    logger.info("Bot is starting...")
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
        handle_signals=True,  # graceful SIGTERM on Railway
        close_bot_session=True,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
    except Exception:
        logger.exception("Fatal error in main()")
        sys.exit(1)
