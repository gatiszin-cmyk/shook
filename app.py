import os
import json
import logging
import requests
from datetime import datetime, timedelta, time, timezone
from urllib.parse import urlparse

from dotenv import load_dotenv
import psycopg2

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Message
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    JobQueue,
    filters,
)

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------- Env ----------
load_dotenv(dotenv_path="runtime.env")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "8088620127"))
SHEET_URL = os.getenv("GOOGLE_SHEET_URL")

# ---------- Google Sheets Logging ----------
def log_to_google_sheets(user):
    if not SHEET_URL:
        return
    try:
        payload = {
            "user_id": user.id,
            "username": f"@{user.username}" if user.username else "N/A",
            "full_name": user.full_name
        }
        requests.post(SHEET_URL, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Sheet logging failed: {e}")

# ---------- DB Helpers ----------
def db_connect():
    p = urlparse(DATABASE_URL)
    conn = psycopg2.connect(dbname=p.path.lstrip("/"), user=p.username, password=p.password, host=p.hostname, port=p.port, sslmode="require")
    conn.autocommit = True
    return conn

def db_init_schema():
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS starts (id SERIAL PRIMARY KEY, user_id BIGINT, started_at TIMESTAMPTZ DEFAULT NOW());")
    logger.info("DB schema ensured.")

# ---------- States ----------
MAIN_MENU, AGENCY_MENU, CLOAKING_MENU = range(3)

# ---------- Links ----------
CLOAKING_URL = "https://socialhook.media/sp/cloaking-course/?utm_source=telegram"
REGISTER_URL = "https://socialhook.media/aurora"
SUPPORT_TELEGRAM_URL = "https://t.me/socialhookagency"

# ---------- Keyboards ----------
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈📱 Agency Ad Account Service", callback_data="main:agency")],
        [InlineKeyboardButton("🎓 Cloaking Course", callback_data="main:cloaking")],
    ])

def agency_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 About Ad Accounts", callback_data="agency:about"),
         InlineKeyboardButton("🛡️ Service & Truspilot", callback_data="agency:aurora")],
        [InlineKeyboardButton("📥 How To Receive", callback_data="agency:howto"),
         InlineKeyboardButton("❓ FAQ", callback_data="agency:faq")],
        [InlineKeyboardButton("🔥 SIGN UP & START FREE TRIAL NOW 🔥", url=REGISTER_URL)],
        [InlineKeyboardButton("💬 Talk To Support", url=SUPPORT_TELEGRAM_URL)],
        [InlineKeyboardButton("⬅️ Back", callback_data="nav:back:main")],
    ])

def cloaking_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👉🔗 Get Cloaking Mastery Now!", url=CLOAKING_URL)],
        [InlineKeyboardButton("⬅️ Back", callback_data="nav:back:main")],
    ])

# ---------- Content ----------
CLOAKING_TEXT = (
    "🔥 Socialhook Cloaking Mastery Course is LIVE!\n"
    "Learn how to run unrestricted ads on Meta & Google!\n"
    "What you'll get:\n"
    "✅ Step-by-step cloaking strategies\n"
    "🛠️ Secret tools & proven methods\n"
    "🌍 Trusted by media buyers worldwide\n"
)

ABOUT_TEXT = (
    "We provide agency ad accounts for Meta, Google, Snapchat, TikTok, Bing, Taboola and Outbrain.\n\n"
    "🛡 Manual credit line agency ad accounts: Higher quality, fewer restrictions.\n"
    "💰 Low top-up fees: 0-3% perfect for scaling.\n"
    "💳 Crypto/Bank/Card options."
)

HOWTO_TEXT = (
    "Step-by-step instructions:\n\n"
    "1️⃣ Register via the link below.\n"
    "2️⃣ Pick a plan (trial activated automatically).\n"
    "3️⃣ Top up balance in \"Wallet\".\n"
    "4️⃣ Request accounts in \"Ad Accounts\".\n"
    "5️⃣ Done! Wait for delivery (up to 3 business days). You can contact tech support directly from the dashboard links.\n"
)

FAQ_TEXT = (
    "❓ Own card? No, use our credit lines.\n"
    "❓ Service Fee? Meta is $300/mo.\n"
    "❓ Min Top Up? Meta $250, Google $1000.\n"
    "❓ Banned? We appeal or refund balance."
)

AURORA_SERVICE_TEXT = (
    "Agency Aurora (since 2021): 350M+ yearly spend.\n\n"
    "🛡️ Whitelisted enterprise accounts.\n"
    "📱 Proprietary self-serve platform.\n"
    "🛠️ Dedicated management.\n\n"
    "Reviews: https://www.trustpilot.com/review/agency-aurora.com"
)

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user:
        log_to_google_sheets(user)
    await update.message.reply_text("Welcome! Choose an option:", reply_markup=main_menu_kb())
    return MAIN_MENU

async def main_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "main:agency":
        await query.edit_message_text("Agency Ad Account Service — choose an option:", reply_markup=agency_menu_kb())
        return AGENCY_MENU
    if query.data == "main:cloaking":
        await query.edit_message_text(CLOAKING_TEXT, reply_markup=cloaking_menu_kb())
        return CLOAKING_MENU
    return MAIN_MENU

async def agency_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    # Back to main
    if query.data == "nav:back:main":
        if query.message.caption: # If it's a photo message, delete it
            await query.message.delete()
            await context.bot.send_message(chat_id=query.message.chat_id, text="Welcome! Choose an option:", reply_markup=main_menu_kb())
        else:
            await query.edit_message_text("Welcome! Choose an option:", reply_markup=main_menu_kb())
        return MAIN_MENU

    # Send Photo for Aurora
    if query.data == "agency:aurora":
        try:
            await query.message.delete()
            with open('aurora-service.jpg', 'rb') as photo:
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=photo,
                    caption=AURORA_SERVICE_TEXT,
                    reply_markup=agency_menu_kb()
                )
        except Exception as e:
            logger.error(f"Image error: {e}")
            await context.bot.send_message(chat_id=query.message.chat_id, text=AURORA_SERVICE_TEXT, reply_markup=agency_menu_kb())
        return AGENCY_MENU

    # Handle Text Updates
    mapping = {"agency:about": ABOUT_TEXT, "agency:howto": HOWTO_TEXT, "agency:faq": FAQ_TEXT}
    if query.data in mapping:
        if query.message.caption: # If currently on a photo message, delete and send new text menu
            await query.message.delete()
            await context.bot.send_message(chat_id=query.message.chat_id, text=mapping[query.data], reply_markup=agency_menu_kb())
        else:
            await query.edit_message_text(mapping[query.data], reply_markup=agency_menu_kb())
    
    return AGENCY_MENU

async def cloaking_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "nav:back:main":
        await query.edit_message_text("Welcome! Choose an option:", reply_markup=main_menu_kb())
        return MAIN_MENU
    return CLOAKING_MENU

def main():
    db_init_schema()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [CallbackQueryHandler(main_menu_router)],
            AGENCY_MENU: [CallbackQueryHandler(agency_router)],
            CLOAKING_MENU: [CallbackQueryHandler(cloaking_router)],
        },
        fallbacks=[CommandHandler("start", start)],
        per_user=True,
        per_chat=True,
        per_message=False # CRITICAL: Keep False so buttons work across different messages
    )

    app.add_handler(conv)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
