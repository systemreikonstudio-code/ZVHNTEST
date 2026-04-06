"""
ZHN Telegram Bot
- Full payment flow: plan selection → payment method → receipt → confirm
- Admin receives ALL user messages in real-time (from /start onwards)
- Admin can reply to forwarded messages and the bot relays them back to the user
- Ping channel receives only a neutral "You have been pinged" message on confirm
"""

import asyncio
import logging
import os
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
PING_CHAT_ID = -1003580782912  # Ping channel ID (hardcoded)

# ---------------------------------------------------------------------------
# Plan definitions
# ---------------------------------------------------------------------------
PLANS = {
    "STARTER": {"label": "STARTER", "price": 300, "duration": "1 Month"},
    "ADVANCE": {"label": "ADVANCE", "price": 900, "duration": "3 Months"},
    "PRO":     {"label": "PRO",     "price": 1500, "duration": "6 Months"},
    "ELITE":   {"label": "ELITE",   "price": 3000, "duration": "12 Months"},
}

# ---------------------------------------------------------------------------
# In-memory state stores
# ---------------------------------------------------------------------------
# user_id → {"plan": str, "payment": str, "waiting_msg_id": int | None}
#   waiting_msg_id: the message_id of the "Searching for a payment address..."
#   message shown to the user after Confirm. Deleted when admin first replies.
user_state: dict[int, dict] = {}

# forwarded_msg_id → user_id  (so admin replies can be routed back to the user)
forwarded_to_user: dict[int, int] = {}

# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def kb_join() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="I want to join the channel", callback_data="join")]
    ])

def kb_plans() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="STARTER", callback_data="plan_STARTER"),
            InlineKeyboardButton(text="ADVANCE", callback_data="plan_ADVANCE"),
        ],
        [
            InlineKeyboardButton(text="PRO",     callback_data="plan_PRO"),
            InlineKeyboardButton(text="ELITE",   callback_data="plan_ELITE"),
        ],
    ])

def kb_payment() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="bKash",  callback_data="pay_bKash"),
            InlineKeyboardButton(text="Nagad",  callback_data="pay_Nagad"),
        ],
        [
            InlineKeyboardButton(text="Rocket", callback_data="pay_Rocket"),
            InlineKeyboardButton(text="Others", callback_data="pay_Others"),
        ],
    ])

def kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Confirm", callback_data="confirm")]
    ])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def forward_to_admin(bot: Bot, text: str, user_id: int) -> None:
    """
    Send a notification to the admin and store the mapping
    forwarded_msg_id → user_id so admin replies route back correctly.
    """
    try:
        sent = await bot.send_message(ADMIN_CHAT_ID, text, parse_mode=ParseMode.HTML)
        forwarded_to_user[sent.message_id] = user_id
        logger.info("Forwarded to admin fwd_id=%s → user_id=%s", sent.message_id, user_id)
    except Exception as exc:
        logger.error("Failed to forward to admin: %s", exc)


def receipt_text(user_id: int, username: str | None, plan_key: str, payment: str) -> str:
    plan = PLANS[plan_key]
    uname = f"@{username}" if username else "No username"
    return (
        "📋 <b>SUMMARY</b>\n\n"
        f"<b>Order ID:</b> {user_id}\n"
        f"<b>Username:</b> {uname}\n"
        f"<b>Plan:</b> {plan['label']} ({plan['duration']})\n"
        f"<b>Amount:</b> {plan['price']} BDT\n"
        f"<b>Payment Method:</b> {payment}\n"
        f"<b>Status:</b> Due"
    )

# ---------------------------------------------------------------------------
# User flow handlers
# ---------------------------------------------------------------------------

async def cmd_start(message: Message, bot: Bot) -> None:
    """Step 1 — Welcome message. Forwarded to admin immediately."""
    user = message.from_user
    logger.info("/start from user_id=%s username=%s", user.id, user.username)

    await forward_to_admin(
        bot,
        f"👤 <b>New /start</b>\n"
        f"User ID: <code>{user.id}</code>\n"
        f"Username: @{user.username or 'none'}\n"
        f"Name: {user.full_name}",
        user.id,
    )

    # Reset state for this user
    user_state[user.id] = {"plan": None, "payment": None, "waiting_msg_id": None}

    await message.answer(
        "Welcome to ZHN. Join the channel to get regular updates.",
        reply_markup=kb_join(),
    )


async def cb_join(callback: CallbackQuery, bot: Bot) -> None:
    """After 'I want to join the channel' — show plan selection."""
    user = callback.from_user
    logger.info("cb_join user_id=%s", user.id)
    await callback.answer()

    await forward_to_admin(
        bot,
        f"🟢 <b>User clicked 'Join Channel'</b>\n"
        f"User ID: <code>{user.id}</code> | @{user.username or 'none'}",
        user.id,
    )

    await callback.message.edit_text(
        "Please select your plan:\n\n"
        "• <b>STARTER</b> — 300 BDT (1 Month)\n"
        "• <b>ADVANCE</b> — 900 BDT (3 Months)\n"
        "• <b>PRO</b> — 1500 BDT (6 Months)\n"
        "• <b>ELITE</b> — 3000 BDT (12 Months)",
        reply_markup=kb_plans(),
        parse_mode=ParseMode.HTML,
    )


async def cb_plan(callback: CallbackQuery, bot: Bot) -> None:
    """Step 2 — Store plan, show payment method selection."""
    user = callback.from_user
    plan_key = callback.data.split("_", 1)[1]
    logger.info("cb_plan user_id=%s plan=%s", user.id, plan_key)
    await callback.answer(f"Plan selected: {plan_key}")

    user_state.setdefault(user.id, {})["plan"] = plan_key

    await forward_to_admin(
        bot,
        f"📦 <b>Plan Selected</b>\n"
        f"User ID: <code>{user.id}</code> | @{user.username or 'none'}\n"
        f"Plan: <b>{plan_key}</b> — {PLANS[plan_key]['price']} BDT",
        user.id,
    )

    await callback.message.edit_text(
        "Please select your payment method:",
        reply_markup=kb_payment(),
    )


async def cb_payment(callback: CallbackQuery, bot: Bot) -> None:
    """Step 3 — Store payment method, show receipt."""
    user = callback.from_user
    payment = callback.data.split("_", 1)[1]
    logger.info("cb_payment user_id=%s payment=%s", user.id, payment)
    await callback.answer(f"Payment: {payment}")

    state = user_state.setdefault(user.id, {})
    state["payment"] = payment
    plan_key = state.get("plan") or "STARTER"

    await forward_to_admin(
        bot,
        f"💳 <b>Payment Method Selected</b>\n"
        f"User ID: <code>{user.id}</code> | @{user.username or 'none'}\n"
        f"Method: <b>{payment}</b>",
        user.id,
    )

    await callback.message.edit_text(
        receipt_text(user.id, user.username, plan_key, payment),
        reply_markup=kb_confirm(),
        parse_mode=ParseMode.HTML,
    )


async def cb_confirm(callback: CallbackQuery, bot: Bot) -> None:
    """
    Step 4 — Confirm:
      A. Show 'Searching for a payment address...' to the user and store
         that message's ID so it can be deleted when admin replies.
      B. Send ONLY a neutral ping to PING_CHAT_ID (no user data).
      C. Notify admin separately (never leaks to ping channel).
    """
    user = callback.from_user
    logger.info("cb_confirm user_id=%s", user.id)
    await callback.answer()

    # A. Edit the receipt message in-place to the waiting text and save its ID
    waiting_msg = await callback.message.edit_text(
        "Searching for a payment address... Please wait..."
    )
    user_state.setdefault(user.id, {})["waiting_msg_id"] = waiting_msg.message_id

    # Typing indicator for realism
    await bot.send_chat_action(user.id, ChatAction.TYPING)
    await asyncio.sleep(1)

    # B. Ping channel — ONLY this neutral text, zero user data
    try:
        await bot.send_message(PING_CHAT_ID, "You have been pinged")
        logger.info("Ping sent to PING_CHAT_ID=%s", PING_CHAT_ID)
    except Exception as exc:
        logger.error("Failed to send ping to PING_CHAT_ID=%s: %s", PING_CHAT_ID, exc)

    # C. Notify admin (completely separate from ping)
    state = user_state.get(user.id, {})
    await forward_to_admin(
        bot,
        f"✅ <b>User Confirmed</b>\n"
        f"User ID: <code>{user.id}</code> | @{user.username or 'none'}\n"
        f"Plan: <b>{state.get('plan', '?')}</b>\n"
        f"Payment: <b>{state.get('payment', '?')}</b>",
        user.id,
    )


# ---------------------------------------------------------------------------
# Admin reply routing
# ---------------------------------------------------------------------------

async def handle_admin_reply(message: Message, bot: Bot) -> None:
    """
    Admin replies to any forwarded message → relay text to the original user.
    If the user still has the 'Searching...' message visible, delete it first
    so the waiting message vanishes and the admin's reply appears cleanly.
    """
    replied_to_id = message.reply_to_message.message_id
    target_user_id = forwarded_to_user.get(replied_to_id)

    if target_user_id is None:
        await message.reply("⚠️ Could not find the user for that message.")
        return

    # Delete the "Searching for a payment address..." message if it's still there
    waiting_msg_id = user_state.get(target_user_id, {}).get("waiting_msg_id")
    if waiting_msg_id:
        try:
            await bot.delete_message(target_user_id, waiting_msg_id)
            logger.info("Deleted waiting message %s for user_id=%s", waiting_msg_id, target_user_id)
        except Exception as exc:
            logger.warning("Could not delete waiting message: %s", exc)
        # Clear it so we only attempt deletion once
        user_state[target_user_id]["waiting_msg_id"] = None

    # Relay the admin's message to the user
    try:
        await bot.send_message(
            target_user_id,
            message.text or message.caption or "(empty)",
        )
        await message.reply(
            f"✅ Message delivered to user <code>{target_user_id}</code>.",
            parse_mode=ParseMode.HTML,
        )
        logger.info("Admin reply relayed to user_id=%s", target_user_id)
    except Exception as exc:
        logger.error("Failed to relay admin reply: %s", exc)
        await message.reply(f"⚠️ Failed to deliver: {exc}")


# ---------------------------------------------------------------------------
# Catch-all user message forwarder
# ---------------------------------------------------------------------------

async def handle_user_message(message: Message, bot: Bot) -> None:
    """Forward every non-command user message to admin in real-time."""
    user = message.from_user
    if user.id == ADMIN_CHAT_ID:
        return  # Don't echo admin's own messages back

    logger.info("User message from user_id=%s", user.id)
    content = message.text or message.caption or "[non-text message]"

    await forward_to_admin(
        bot,
        f"💬 <b>User Message</b>\n"
        f"User ID: <code>{user.id}</code> | @{user.username or 'none'}\n"
        f"Message: {content}",
        user.id,
    )


# ---------------------------------------------------------------------------
# Admin utility commands
# ---------------------------------------------------------------------------

async def cmd_admin_delete(message: Message, bot: Bot) -> None:
    """
    /delete (reply to a forwarded message) — remove it from the admin chat.
    Only works when sent by the admin.
    """
    if message.from_user.id != ADMIN_CHAT_ID:
        return

    if not message.reply_to_message:
        await message.reply("Reply to a forwarded user message to delete it.")
        return

    replied_to_id = message.reply_to_message.message_id
    if forwarded_to_user.get(replied_to_id) is None:
        await message.reply("Could not identify the original user message.")
        return

    try:
        await bot.delete_message(ADMIN_CHAT_ID, replied_to_id)
        await message.reply("🗑️ Message deleted.")
    except Exception as exc:
        await message.reply(f"⚠️ Could not delete: {exc}")


async def cmd_admin_send(message: Message, bot: Bot) -> None:
    """
    /send <user_id> <text> — send a manual message to any user.
    Only works when sent by the admin.
    """
    if message.from_user.id != ADMIN_CHAT_ID:
        return

    parts = (message.text or "").split(None, 2)
    if len(parts) < 3:
        await message.reply("Usage: /send <user_id> <message text>")
        return

    try:
        target_id = int(parts[1])
        await bot.send_message(target_id, parts[2])
        await message.reply(
            f"✅ Sent to <code>{target_id}</code>.", parse_mode=ParseMode.HTML
        )
    except ValueError:
        await message.reply("Invalid user ID.")
    except Exception as exc:
        await message.reply(f"⚠️ Failed: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Always clear any leftover webhook before starting long polling
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Webhook cleared, starting long polling...")

    dp = Dispatcher()

    # User flow
    dp.message.register(cmd_start, Command("start"))
    dp.callback_query.register(cb_join,     F.data == "join")
    dp.callback_query.register(cb_plan,     F.data.startswith("plan_"))
    dp.callback_query.register(cb_payment,  F.data.startswith("pay_"))
    dp.callback_query.register(cb_confirm,  F.data == "confirm")

    # Admin commands (registered before the catch-all)
    dp.message.register(cmd_admin_delete, Command("delete"), F.chat.id == ADMIN_CHAT_ID)
    dp.message.register(cmd_admin_send,   Command("send"),   F.chat.id == ADMIN_CHAT_ID)

    # Admin reply routing (before generic catch-all)
    dp.message.register(
        handle_admin_reply,
        F.chat.id == ADMIN_CHAT_ID,
        F.reply_to_message.as_("reply_to_message"),
    )

    # Catch-all: forward every user message to admin
    dp.message.register(handle_user_message)

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
