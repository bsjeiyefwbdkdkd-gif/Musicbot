"""
Telegram forwarder bot — colored "ارسال کد" button.

Flow:
  1. User runs /start → bot shows a blue inline button "ارسال کد"
  2. User clicks → bot enters waiting_for_code state
  3. User sends a message (text/photo/video/doc/voice/sticker) → bot forwards
     it to the admin together with the sender's info
  4. Admin replies to the forwarded message → bot sends the reply back to user
  5. User can /cancel any time

Tested with aiogram 3.30.0 / Python 3.11+ / Bot API 9.4.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# admin's chat message id  →  original user id
# (so admin can reply to either the "info" message or the forwarded message)
user_by_admin_msg: dict[int, int] = {}


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
# Handlers
# --------------------------------------------------------------------------- #


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "سلام! 👋\n"
        "برای ارسال کد روی دکمه‌ی زیر بزن:",
        reply_markup=main_keyboard(),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("لغو شد. هر وقت خواستی دوباره /start بزن.")


@router.callback_query(F.data == "send_code")
async def on_send_code(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserStates.waiting_for_code)
    if callback.message:
        try:
            await callback.message.edit_text("بفرست کدت رو 👇")
        except Exception:
            # Message is too old or already edited — send a new one
            await callback.message.answer("بفرست کدت رو 👇")
    await callback.answer()


@router.message(UserStates.waiting_for_code)
async def receive_code(message: Message, state: FSMContext) -> None:
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
    user_by_admin_msg[info_msg.message_id] = user.id

    # 2) Forward the actual user message (text/media)
    sent_msg_id: int | None = None
    try:
        fwd = await bot.forward_message(
            chat_id=ADMIN_ID,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        sent_msg_id = fwd.message_id
    except Exception as exc:
        logger.warning("forward_message failed (%s), trying copy_message", exc)
        try:
            cp = await bot.copy_message(
                chat_id=ADMIN_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            sent_msg_id = cp.message_id
        except Exception as exc2:
            logger.warning("copy_message also failed (%s)", exc2)
            if message.text:
                sent = await bot.send_message(
                    ADMIN_ID, f"💬 {message.text}"
                )
                sent_msg_id = sent.message_id

    if sent_msg_id is not None:
        user_by_admin_msg[sent_msg_id] = user.id

    await message.answer("✅ کدت ارسال شد! منتظر جواب باشم.")
    await state.clear()


@router.message(F.reply_to_message)
async def admin_reply(message: Message) -> None:
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        return

    user_id = user_by_admin_msg.get(message.reply_to_message.message_id)
    if user_id is None:
        return  # Admin replied to a non-tracked message

    try:
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
    except Exception as exc:
        logger.exception("Failed to forward admin reply")
        await message.reply(f"❌ خطا در ارسال: {exc}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def main() -> None:
    logger.info("Bot is starting...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
