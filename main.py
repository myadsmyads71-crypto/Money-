import os
import re
import html
import logging
import threading

import httpx
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ---------- CONFIG (sab Render ke Environment Variables me) ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])

def _ch(v):
    v = v.strip()
    return int(v) if re.fullmatch(r"-?\d+", v) else v  # @username ya -100xxxx

CHANNELS = [
    (_ch(os.environ["CHANNEL_1"]), os.environ["LINK_1"]),
    (_ch(os.environ["CHANNEL_2"]), os.environ["LINK_2"]),
]

# Dusra bot (optional): isko bhi same message jayega
BOT2_TOKEN = os.getenv("BOT2_TOKEN")
BOT2_CHAT_ID = os.getenv("BOT2_CHAT_ID")

# ---------- IN-MEMORY (database nahi) ----------
notified = set()   # jin users ka admin ko message ja chuka
route = {}         # admin message id -> user id (reply ke liye)

OK_STATUS = (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def e(x):
    return html.escape(str(x))


async def missing_channels(bot, uid):
    missing = []
    for i, (ch, _) in enumerate(CHANNELS):
        try:
            m = await bot.get_chat_member(ch, uid)
            ok = m.status in OK_STATUS or (
                m.status == ChatMemberStatus.RESTRICTED and getattr(m, "is_member", False)
            )
        except TelegramError as ex:
            log.warning("check fail %s: %s", ch, ex)
            ok = False
        if not ok:
            missing.append(i)
    return missing


def join_keyboard(ref):
    rows = [[InlineKeyboardButton(f"📢 Channel {i + 1} Join Karo", url=link)]
            for i, (_, link) in enumerate(CHANNELS)]
    rows.append([InlineKeyboardButton("✅ Joined, Check Karo", callback_data=f"chk:{ref or 0}")])
    return InlineKeyboardMarkup(rows)


async def notify_admins(bot, text):
    """Admin ko + dusre bot ko message bhejo."""
    try:
        sent = await bot.send_message(ADMIN_ID, text, parse_mode=ParseMode.HTML)
        return sent
    except TelegramError as ex:
        log.error("admin send fail: %s", ex)
    finally:
        if BOT2_TOKEN and BOT2_CHAT_ID:
            try:
                async with httpx.AsyncClient(timeout=15) as c:
                    await c.post(
                        f"https://api.telegram.org/bot{BOT2_TOKEN}/sendMessage",
                        data={"chat_id": BOT2_CHAT_ID, "text": text, "parse_mode": "HTML"},
                    )
            except Exception as ex:
                log.error("bot2 send fail: %s", ex)


async def verified(update_user, chat_id, ref, context):
    uid = update_user.id
    if ref == uid:
        ref = None
    if uid not in notified:
        notified.add(uid)
        text = (
            f"➕ add new member <code>{uid}</code>\n"
            f"🔗 With refer code <code>{e(ref) if ref else 'none'}</code>"
        )
        sent = await notify_admins(context.bot, text)
        if sent:
            route[sent.message_id] = uid
    me = await context.bot.get_me()
    await context.bot.send_message(
        chat_id,
        "🎉 Verify ho gaya! Welcome.\n\n"
        f"Aapka refer link:\nhttps://t.me/{me.username}?start={uid}",
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ref = None
    if context.args and context.args[0].isdigit():
        ref = int(context.args[0])
    if user.id == ADMIN_ID:
        await update.message.reply_text(
            "👑 Admin mode.\nUser ko reply karke message bhejo, ya:\n/msg USER_ID text"
        )
        return
    if await missing_channels(context.bot, user.id):
        await update.message.reply_text(
            "Bot use karne ke liye pehle dono channel join karo 👇",
            reply_markup=join_keyboard(ref),
        )
        return
    await verified(user, update.effective_chat.id, ref, context)


async def check_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    ref = int(q.data.split(":")[1]) or None
    miss = await missing_channels(context.bot, q.from_user.id)
    if miss:
        names = ", ".join(f"Channel {i + 1}" for i in miss)
        await q.answer(f"❌ Abhi join nahi kiya: {names}", show_alert=True)
        return
    await q.answer("✅ Verified")
    try:
        await q.message.delete()
    except TelegramError:
        pass
    await verified(q.from_user, q.message.chat_id, ref, context)


async def user_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User ka message admin tak (sirf verified users)."""
    u = update.effective_user
    if u.id not in notified:
        await update.message.reply_text("Pehle /start dabao aur channels join karo.")
        return
    copied = await update.message.copy(ADMIN_ID)
    route[copied.message_id] = u.id
    head = await context.bot.send_message(
        ADMIN_ID, f"📩 Message from <code>{u.id}</code> (reply karke jawab do)",
        parse_mode=ParseMode.HTML, reply_to_message_id=copied.message_id,
    )
    route[head.message_id] = u.id


async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = update.message.reply_to_message
    uid = route.get(r.message_id)
    if not uid:  # bot restart ke baad: text me se ID nikalo
        m = re.search(r"(?:member|from)\s+(\d{5,})", r.text or "")
        uid = int(m.group(1)) if m else None
    if not uid:
        await update.message.reply_text("User ID nahi mili. /msg USER_ID text use karo.")
        return
    try:
        await update.message.copy(uid)
        await update.message.reply_text("✅ Bhej diya")
    except TelegramError as ex:
        await update.message.reply_text(f"❌ Fail: {ex}")


async def msg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text("Use: /msg USER_ID message")
        return
    try:
        await context.bot.send_message(int(context.args[0]), " ".join(context.args[1:]))
        await update.message.reply_text("✅ Bhej diya")
    except TelegramError as ex:
        await update.message.reply_text(f"❌ Fail: {ex}")


# ---------- Render ko jagaye rakhne ke liye mini web server ----------
web = Flask(__name__)

@web.route("/")
def home():
    return "Bot is running", 200

def run_web():
    web.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))


def main():
    threading.Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    admin = filters.User(ADMIN_ID)
    app.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("msg", msg_cmd, admin))
    app.add_handler(CallbackQueryHandler(check_cb, pattern=r"^chk:\d+$"))
    app.add_handler(MessageHandler(admin & filters.REPLY & ~filters.COMMAND, admin_reply))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~admin & ~filters.COMMAND, user_msg))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
