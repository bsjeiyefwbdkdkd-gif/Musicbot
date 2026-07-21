"""
Simple Telegram forwarder bot with a colored "ارسال کد" button.

Features:
- Inline button with blue color (style="primary") — Bot API 9.4 / aiogram 3.30+
- User clicks the button → bot asks for a code
- User sends the code → bot forwards it to the admin
- Admin can reply directly to the forwarded message; bot sends reply back to the user
- Supports text, photo, video, document, voice, stickers — anything copy_message supports
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
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

# ----------------------------- CONFIG --------------------------------------- #
# Put these in environment variables in production.
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))  # your numeric Telegram user id

# ----------------------------- BOT SETUP ------------------------------------ #
logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Maps a message id in the admin's chat -> original user id.
# Both the "info" message and the forwarded/copied message point to the same user,
# so the admin can reply to either and the bot will route it correctly.
user_by_admin_msg: dict[int, int] = {}


class UserStates(StatesGroup):
    waiting_for_code = State()


# ----------------------------- HANDLERS ------------------------------------- #
@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Welcome message with the colored 'ارسال کد' button."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="ارسال کد",
                    callback_data="send_code",
                    style="primary",  # 🔵 blue — Bot API 9.4 feature
                )
            ]
        ]
    )
    await message.answer(
        "سلام! 👋\n"
        "برای ارسال کد روی دکمه‌ی زیر بزن:",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "send_code")
async def on_send_code(callback: CallbackQuery, state: FSMContext) -> None:
    """User pressed the button — switch them into 'waiting for code' state."""
    await state.set_state(UserStates.waiting_for_code)
    if callback.message:
        await callback.message.edit_text("بفرست کدت رو 👇")
    await callback.answer()


@router.message(UserStates.waiting_for_code)
async def receive_code(message: Message, state: FSMContext) -> None:
    """User sent their code — forward to admin with sender info."""
    user = message.from_user
    if user is None:
        return

    # 1) Send a header message with the sender's info
    info_text = (
        "📩 پیام جدید از کاربر:\n"
        f"👤 نام: {user.full_name}\n"
        f"🆔 آیدی: <code>{user.id}</code>\n"
        f"🔗 یوزرنیم: @{user.username or 'ندارد'}\n"
        "─────────────────"
    )
    info_msg = await bot.send_message(ADMIN_ID, info_text)
    user_by_admin_msg[info_msg.message_id] = user.id

    # 2) Forward the actual message (works for text, photo, doc, voice, etc.)
    try:
        forwarded = await bot.forward_message(
            chat_id=ADMIN_ID,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        user_by_admin_msg[forwarded.message_id] = user.id
    except Exception:
        # If forward fails (e.g. user has forwarding disabled), fall back to copy / text
        try:
            copied = await bot.copy_message(
                chat_id=ADMIN_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            user_by_admin_msg[copied.message_id] = user.id
        except Exception:
            if message.text:
                sent = await bot.send_message(ADMIN_ID, f"💬 {message.text}")
                user_by_admin_msg[sent.message_id] = user.id

    await message.answer("✅ کدت ارسال شد! منتظر جواب باشم.")
    await state.clear()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """User can cancel with /cancel."""
    await state.clear()
    await message.answer("لغو شد. هر وقت خواستی دوباره /start بزن.")


@router.message(F.reply_to_message)
async def admin_reply(message: Message) -> None:
    """Admin replied to a forwarded message — route the reply to the original user."""
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        return

    target_id = message.reply_to_message.message_id
    user_id = user_by_admin_msg.get(target_id)
    if user_id is None:
        # Admin replied to a non-tracked message — ignore silently
        return

    try:
        if message.text:
            await bot.send_message(user_id, f"💬 پاسخ ادمین:\n\n{message.text}")
        else:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=ADMIN_ID,
                message_id=message.message_id,
            )
        await message.reply("✅ پاسخ ارسال شد.")
    except Exception as exc:  # pragma: no cover
        await message.reply(f"❌ خطا در ارسال پاسخ: {exc}")


# ----------------------------- ENTRYPOINT ----------------------------------- #
async def main() -> None:
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
