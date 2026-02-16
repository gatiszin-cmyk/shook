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

# Log startup status for Sheet URL
if SHEET_URL:
    logger.info(f"✅ GOOGLE_SHEET_URL found: {SHEET_URL[:15]}...")
else:
    logger.error("❌ GOOGLE_SHEET_URL NOT FOUND IN VARIABLES")

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
        logger.info(f"User {user.id} logged to TG_LEADS.")
    except Exception as e:
        logger.error(f"Sheet logging failed: {e}")

# ---------- DB Helpers (Condensed) ----------
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
MAIN_MENU, AGENCY_MENU = range(2)

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
        [InlineKeyboardButton("📝 About Ad Accounts", callback_data="agency:about")],
        [InlineKeyboardButton("🛡️ About Aurora Service + Trustpilot", callback_data="agency:aurora")],        
        [InlineKeyboardButton("📥 How To Receive Ad Accounts", callback_data="agency:howto")],
        [InlineKeyboardButton("❓ FAQ", callback_data="agency:faq")],
        [InlineKeyboardButton("💬 Talk To Support", url=SUPPORT_TELEGRAM_URL)],
        [InlineKeyboardButton("👉🔗 Sign Up & Start FREE TRIAL Now", url=REGISTER_URL)],
        [InlineKeyboardButton("⬅️ Back", callback_data="nav:back:main")],
    ])

# ---------- Content ----------
ABOUT_TEXT = "We provide agency ad accounts for Meta, Google, Snapchat, TikTok, Bing, Taboola and Outbrain..."
HOWTO_TEXT = "1️⃣ Register... 2️⃣ Pick a plan... 3️⃣ Top up... 4️⃣ Request accounts!"
FAQ_TEXT = "❓Can I add my own card? No. ❓Service Fee? Meta: $300/mo."
AURORA_SERVICE_TEXT = "Agency Aurora (since 2021): Whitelisted enterprise accounts, proprietary platform."

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
    return MAIN_MENU

async def agency_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data == "nav:back:main":
        await query.edit_message_text("Welcome! Choose an option:", reply_markup=main_menu_kb())
        return MAIN_MENU
    
    mapping = {
        "agency:about": ABOUT_TEXT,
        "agency:howto": HOWTO_TEXT,
        "agency:faq": FAQ_TEXT,
        "agency:aurora": AURORA_SERVICE_TEXT
    }
    
    if query.data in mapping:
        await query.edit_message_text(mapping[query.data], reply_markup=agency_menu_kb())
    
    return AGENCY_MENU

def main():
    db_init_schema()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [CallbackQueryHandler(main_menu_router)],
            AGENCY_MENU: [CallbackQueryHandler(agency_router)],
        },
        fallbacks=[CommandHandler("start", start)],
        per_message=False, # Fixes the PTB warning
        per_user=True,
        per_chat=True
    )

    app.add_handler(conv)
    logger.info("Bot is polling. Sheet logging active.")
    app.run_polling(drop_pending_updates=True) # Clears backlog on restart

if __name__ == "__main__":
    main()
