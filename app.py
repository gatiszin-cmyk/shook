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

# ---------- Logging (Docker/Railway friendly) ----------
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

# DEBUG: This will show in your Railway logs exactly what the bot sees
SHEET_URL = os.getenv("GOOGLE_SHEET_URL")
if SHEET_URL:
    logger.info(f"✅ GOOGLE_SHEET_URL found: {SHEET_URL[:15]}...") 
else:
    logger.error("❌ GOOGLE_SHEET_URL NOT FOUND IN ENVIRONMENT VARIABLES")

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "8088620127"))
# Google Sheet Web App URL from Railway Variables
SHEET_URL = os.getenv("GOOGLE_SHEET_URL")

# Optional: local timezone for scheduling the daily job
LOCAL_TZ = os.getenv("LOCAL_TZ", "Europe/Riga")

# ---------- Google Sheets Logging Function ----------
def log_to_google_sheets(user):
    if not SHEET_URL:
        logger.warning("GOOGLE_SHEET_URL is not set. Skipping sheet log.")
        return
    try:
        payload = {
            "user_id": user.id,
            "username": f"@{user.username}" if user.username else "N/A",
            "full_name": user.full_name
        }
        # Sends data to the Apps Script Web App
        response = requests.post(SHEET_URL, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info(f"User {user.id} logged to TG_LEADS tab.")
        else:
            logger.error(f"Sheet logging failed with status {response.status_code}")
    except Exception as e:
        logger.error(f"Sheet logging error: {e}")

# ---------- DB ----------
_db_conn = None

def db_connect():
    global _db_conn
    if _db_conn is not None:
        return _db_conn
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing. Add it in Railway service Variables.")
    p = urlparse(DATABASE_URL)
    _db_conn = psycopg2.connect(
        dbname=p.path.lstrip("/"),
        user=p.username,
        password=p.password,
        host=p.hostname,
        port=p.port,
        sslmode="require",
    )
    _db_conn.autocommit = True
    logger.info("DB connected host=%s db=%s user=%s", p.hostname, p.path.lstrip("/"), p.username)
    return _db_conn

def db_init_schema():
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            section TEXT NOT NULL,
            admin_msg_id BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_admin_msg_id ON tickets(admin_msg_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_created_at ON tickets(created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_section ON tickets(section);")
        
        cur.execute("""
        CREATE TABLE IF NOT EXISTS starts (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_starts_started_at ON starts(started_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_starts_user_id ON starts(user_id);")
        
        cur.execute("""
        CREATE TABLE IF NOT EXISTS support_sessions (
            user_id BIGINT PRIMARY KEY,
            section TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_activity TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_support_sessions_last_activity ON support_sessions(last_activity);")
        
    logger.info("DB schema ensured.")

def db_save_ticket(user_id: int, section: str, admin_msg_id: int):
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tickets (user_id, section, admin_msg_id) VALUES (%s, %s, %s) RETURNING ticket_id;",
            (user_id, section, admin_msg_id),
        )
        row = cur.fetchone()
        ticket_id = row[0] if row else None
    return ticket_id

def db_save_start(user_id: int):
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO starts (user_id) VALUES (%s);", (user_id,))

def db_start_support_session(user_id: int, section: str):
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO support_sessions (user_id, section) 
               VALUES (%s, %s) 
               ON CONFLICT (user_id) 
               DO UPDATE SET section = %s, last_activity = NOW();""",
            (user_id, section, section)
        )

def db_get_support_session(user_id: int):
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT section FROM support_sessions 
               WHERE user_id = %s AND last_activity > NOW() - INTERVAL '24 hours';""",
            (user_id,)
        )
        row = cur.fetchone()
    return row[0] if row else None

def db_end_support_session(user_id: int):
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM support_sessions WHERE user_id = %s;", (user_id,))

def db_get_ticket_by_admin_msg_id(admin_msg_id: int):
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ticket_id, user_id, section FROM tickets WHERE admin_msg_id=%s;",
            (admin_msg_id,),
        )
        row = cur.fetchone()
    if not row: return None
    return {"ticket_id": row[0], "user_id": row[1], "section": row[2]}

# ---------- States ----------
MAIN_MENU, AGENCY_MENU, CLOAKING_MENU = range(3)

# ---------- Links ----------
CLOAKING_URL = "https://socialhook.media/sp/cloaking-course/?utm_source=telegram"
REGISTER_URL = "https://socialhook.media/aurora"
SUPPORT_TELEGRAM_URL = "https://t.me/socialhookagency"

# ---------- Keyboards ----------
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈📱 Agency Ad Account Service", callback_data="main:agency")],
        [InlineKeyboardButton("🎓 Cloaking Course", callback_data="main:cloaking")],
    ])

def agency_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 About Ad Accounts", callback_data="agency:about")],
        [InlineKeyboardButton("🛡️ About Aurora Service + Trustpilot", callback_data="agency:aurora")],        
        [InlineKeyboardButton("📥 How To Receive Ad Accounts", callback_data="agency:howto")],
        [InlineKeyboardButton("❓ FAQ", callback_data="agency:faq")],
        # Combined Support and Schedule Option
        [InlineKeyboardButton("💬 Talk To Support", url=SUPPORT_TELEGRAM_URL)],
        [InlineKeyboardButton("👉🔗 Sign Up & Start FREE TRIAL Now", url=REGISTER_URL)],
        [InlineKeyboardButton("⬅️ Back", callback_data="nav:back:main")],
    ])

def cloaking_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👉🔗 Get Cloaking Mastery Now!", url=CLOAKING_URL)],
        [InlineKeyboardButton("⬅️ Back", callback_data="nav:back:main")],
    ])

def back_with_register_kb(back_target: str = "agency") -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("👉🔗 Sign Up & Start FREE TRIAL Now", url=REGISTER_URL)]]
    if back_target == "agency":
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="nav:back:agency")])
    else:
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="nav:back:main")])
    return InlineKeyboardMarkup(rows)

# ---------- Content ----------
CLOAKING_TEXT = (
    "🔥 Socialhook Cloaking Mastery Course is LIVE!\n"
    "Learn how to run unrestricted ads on Meta & Google!\n"
    "What you'll get:\n"
    "✅ Step-by-step cloaking strategies\n"
    "🛠️ Secret tools & proven methods\n"
    "🌍 Trusted by media buyers worldwide\n"
    "💡 Perfect for affiliates, media buyers & marketers who want to scale FAST ⚡\n"
    "🎯 Stop wasting time and money on unsuccessful ads and start running profitable campaigns!\n"
    "Reviews: https://www.blackhatworld.com/seo/cloaking-mastery-bypass-ad-restrictions-scale-any-niche-20-off-for-bhw-meta-google.1729255/\n"
)

ABOUT_TEXT = (
    "We provide agency ad accounts for Meta, Google, Snapchat, TikTok, Bing, Taboola and Outbrain.\n\n"
    "Here are some benefits of advertising on our ad accounts:\n"
    "🛡 Manual credit line agency ad accounts: Higher quality ad accounts mean fewer restrictions. "
    "Our clients report longer account lifespans and better approval rates than with their previous suppliers.\n"
    "💰 Low top-up fees: Enjoy 0-3% top-up fees for all major platforms. Negotiable for big spenders.\n"
    "💳 Bank/Credit Card/Crypto balance top-up options.\n"
    "🎁 Spend 100k across 3 months, and fees are fully refunded + up to 4% cashback.\n\n"
    "The ad accounts are made in HK, usable globally. Currency is USD."
)

HOWTO_TEXT = (
    "Step-by-step instructions:\n\n"
    "1️⃣ Register to the self-service platform via the link below.\n"
    "2️⃣ Pick a plan (trial activated automatically).\n"
    "3️⃣ Top up balance in the \"Wallet\" section.\n"
    "4️⃣ Request ad accounts through the \"Ad Accounts\" section.\n"
    "✅ Done! Wait for delivery and communicate with your manager in the Support section."
)

FAQ_TEXT = (
    "FAQ:\n"
    "❓Can I add my own card?\nNo, these are credit line accounts. Top up via our dashboard.\n"
    "❓Do you provide assets?\nWe share accounts to your BM. We can provide BMs for Meta if needed.\n"
    "❓Service Fee?\nMeta: $300/mo + 0-3% top-up fee. Fees refunded if $100k spent in 3 months.\n"
    "❓Minimum Top Up?\nMeta $250, Google/TikTok $1000.\n"
    "❓Banned accounts?\nWe appeal or refund unused balance in 1-2 days.\n"
)

AURORA_SERVICE_TEXT = (
    "Agency Aurora (since 2021): 350M+ yearly spend, 3000+ advertisers.\n\n"
    "🛡️ Whitelisted enterprise accounts\n"
    "📱 Proprietary self-serve platform\n"
    "🛠️ Dedicated account management\n\n"
    "Reviews: https://www.trustpilot.com/review/agency-aurora.com"
)

ACK_TEXT = "Thanks! Our team will get back to you here shortly."

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    try:
        if user:
            # 1. Log to Internal DB
            db_save_start(user.id)
            db_end_support_session(user.id)
            # 2. Log to External Google Sheet
            log_to_google_sheets(user)
    except Exception as e:
        logger.error("Failed to log /start: %s", e)

    context.user_data.clear()
    await update.message.reply_text("Welcome! Choose an option:", reply_markup=main_menu_kb())
    return MAIN_MENU

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "main:agency":
        await query.edit_message_text("Agency Ad Account Service — choose an option:", reply_markup=agency_menu_kb())
        return AGENCY_MENU
    if data == "main:cloaking":
        await query.edit_message_text(CLOAKING_TEXT, reply_markup=cloaking_menu_kb())
        return CLOAKING_MENU
    return MAIN_MENU

async def agency_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "nav:back:main":
        await query.edit_message_text("Welcome! Choose an option:", reply_markup=main_menu_kb())
        return MAIN_MENU
    
    if data == "nav:back:agency":
        try:
            await query.message.delete()
        except: pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Agency Ad Account Service — choose an option:",
            reply_markup=agency_menu_kb()
        )
        return AGENCY_MENU

    if data == "agency:about":
        await query.edit_message_text(ABOUT_TEXT, reply_markup=back_with_register_kb("agency"))
    elif data == "agency:howto":
        await query.edit_message_text(HOWTO_TEXT, reply_markup=back_with_register_kb("agency"))
    elif data == "agency:faq":
        await query.edit_message_text(FAQ_TEXT, reply_markup=back_with_register_kb("agency"))
    elif data == "agency:aurora":
        try:
            # Note: Ensure aurora-service.jpg exists in your project root
            with open('aurora-service.jpg', 'rb') as photo:
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=photo,
                    caption=AURORA_SERVICE_TEXT,
                    reply_markup=back_with_register_kb("agency")
                )
            await query.message.delete()
        except Exception as e:
            logger.error(f"Image load error: {e}")
            await query.edit_message_text(AURORA_SERVICE_TEXT, reply_markup=back_with_register_kb("agency"))
    
    return AGENCY_MENU

async def cloaking_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "nav:back:main":
        await query.edit_message_text("Welcome! Choose an option:", reply_markup=main_menu_kb())
        return MAIN_MENU
    return CLOAKING_MENU

# ---------- Internal Support logic (Legacy support for active tickets) ----------
async def capture_user_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type != "private": return
    user = update.effective_user
    if not user: return

    db_section = db_get_support_session(user.id)
    if not db_section: return

    msg: Message = update.effective_message
    header = f"[{db_section}] From @{user.username or user.id}:\n{msg.text}"
    header_msg = await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=header)
    db_save_ticket(user.id, db_section, header_msg.message_id)
    await msg.reply_text(ACK_TEXT)

async def cmd_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.id != ADMIN_CHAT_ID: return
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text("Reply to a ticket message with /reply <text>")
        return
    ticket = db_get_ticket_by_admin_msg_id(msg.reply_to_message.message_id)
    if not ticket:
        await msg.reply_text("Ticket not found.")
        return
    args_text = msg.text.removeprefix("/reply").strip()
    if args_text:
        await context.bot.send_message(chat_id=ticket["user_id"], text=args_text)
        await msg.reply_text(f"Sent to user {ticket['user_id']}.")

def main() -> None:
    db_init_schema()
    # Adding 'base_url' check or just ensuring standard build
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [CallbackQueryHandler(main_menu)],
            AGENCY_MENU: [CallbackQueryHandler(agency_router)],
            CLOAKING_MENU: [CallbackQueryHandler(cloaking_router)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
        per_message=True, # Adjusted this to fix the PTBUserWarning from your logs
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("reply", cmd_reply))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), capture_user_text))
    
    logger.info("Bot is polling. Make sure no other instances are running!")
    app.run_polling(drop_pending_updates=True) # This clears old messages stuck in queue

if __name__ == "__main__":
    main()
