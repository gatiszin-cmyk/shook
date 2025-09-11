import os
import json
import logging
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
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Put it in runtime.env as BOT_TOKEN=...")

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "8088620127"))

# Optional: local timezone for scheduling the daily job (report covers previous UTC day)
LOCAL_TZ = os.getenv("LOCAL_TZ", "Europe/Riga")

# ---------- DB ----------
_db_conn = None


def db_connect():
    global _db_conn
    if _db_conn is not None:
        return _db_conn
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing. Add it in Railway service Variables.")
    p = urlparse(DATABASE_URL)  # postgres://user:pass@host:port/db
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
        # Tickets for Support/Schedule messages
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
        
        # Starts for /start analytics
        cur.execute("""
        CREATE TABLE IF NOT EXISTS starts (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_starts_started_at ON starts(started_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_starts_user_id ON starts(user_id);")
        
        # Support sessions for persistent conversation tracking
        cur.execute("""
        CREATE TABLE IF NOT EXISTS support_sessions (
            user_id BIGINT PRIMARY KEY,
            section TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_activity TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_support_sessions_last_activity ON support_sessions(last_activity);")
        
    logger.info("DB schema ensured (tickets, starts, support_sessions & indexes).")

def db_save_ticket(user_id: int, section: str, admin_msg_id: int):
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tickets (user_id, section, admin_msg_id) VALUES (%s, %s, %s) RETURNING ticket_id;",
            (user_id, section, admin_msg_id),
        )
        row = cur.fetchone()  # -> (ticket_id,)
        ticket_id = row[0] if row else None
    logger.info("DB saved ticket ticket_id=%s user_id=%s section=%s admin_msg_id=%s",
                ticket_id, user_id, section, admin_msg_id)
    return ticket_id

def db_save_start(user_id: int):
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO starts (user_id) VALUES (%s);", (user_id,))
    logger.info("DB logged /start from user_id=%s", user_id)

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
    logger.info("Started support session for user_id=%s section=%s", user_id, section)

def db_get_support_session(user_id: int):
    conn = db_connect()
    with conn.cursor() as cur:
        # Get active sessions (within last 24 hours)
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
    logger.info("Ended support session for user_id=%s", user_id)

def db_get_ticket_by_admin_msg_id(admin_msg_id: int):
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ticket_id, user_id, section FROM tickets WHERE admin_msg_id=%s;",
            (admin_msg_id,),
        )
        row = cur.fetchone()
    
    logger.info("DB lookup admin_msg_id=%s => row=%s", admin_msg_id, row)
    
    if not row or len(row) != 3:
        logger.error("Unexpected DB row shape for admin_msg_id=%s: %s", admin_msg_id, row)
        return None
    
    return {
        "ticket_id": row[0],    # First column: ticket_id
        "user_id": row[1],      # Second column: user_id  
        "section": row[2]       # Third column: section
    }

# ---------- States ----------
MAIN_MENU, AGENCY_MENU, CLOAKING_MENU = range(3)

# ---------- Links ----------
CLOAKING_URL = "https://socialhook.media/sp/cloaking-course/?utm_source=telegram"
REGISTER_URL = "https://socialhook.media/aurora"

# Convert Google Drive sharing URL to direct download URL
AURORA_SERVICE_IMAGE_URL = "https://drive.google.com/uc?export=download&id=1Imfng-TzJq0CKvxFSk7w6sg6lNzw-Ecm"

# ---------- Keyboards ----------
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈📱 Agency Ad Account Service", callback_data="main:agency")],
        [InlineKeyboardButton("🎓 Cloaking Course", callback_data="main:cloaking")],
    ])

def agency_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 About Ad Accounts", callback_data="agency:about")],
        [InlineKeyboardButton("📥 How To Receive Ad Accounts", callback_data="agency:howto")],
        [InlineKeyboardButton("❓ FAQ", callback_data="agency:faq")],
        [InlineKeyboardButton("🛡️ About Aurora Service", callback_data="agency:aurora")],
        [InlineKeyboardButton("📅📞 Schedule a Call", callback_data="agency:schedule")],
        [InlineKeyboardButton("💬🤝 Talk To Support", callback_data="agency:support")],
        [InlineKeyboardButton("🔗 Register & Start Free Trial Now!", url=REGISTER_URL)],
        [InlineKeyboardButton("⬅️ Back", callback_data="nav:back:main")],
    ])

def cloaking_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Get Cloaking Mastery Now!", url=CLOAKING_URL)],
        [InlineKeyboardButton("⬅️ Back", callback_data="nav:back:main")],
    ])

def back_with_register_kb(back_target: str = "agency") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔗 Register & Start Free Trial Now!", url=REGISTER_URL)],
    ]
    if back_target == "agency":
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="nav:back:agency")])
    else:
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="nav:back:main")])
    return InlineKeyboardMarkup(rows)

def back_only_kb(back_target: str) -> InlineKeyboardMarkup:
    if back_target == "main":
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="nav:back:main")]])
    if back_target == "agency":
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="nav:back:agency")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="nav:back:main")]])

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
    "Our clients report longer account lifespans and better approval rates than with their previous suppliers with card paid accounts.\n"
    "💰 Low top-up fees: Enjoy 0-3% top-up fees for all major platforms, perfect for scaling your ads. Negotiable for big spenders.\n"
    "💳 Bank/Credit Card/Crypto balance top-up options: Choose the payment method that works best for you.\n"
    "🎁 If you spend 100k across 3 months, the fees will be fully refunded and you can receive up to 4% cashback on ad spend. "
    "Get rewarded for your advertising investment!\n\n"
    "The ad accounts are from HK, but you can run ads targeting any country in the world.\n\n"
    "We don't provide aged or warmed up accounts. All accounts are newly made based on your requests.\n"
)

HOWTO_TEXT = (
    "Step-by-step instructions to start our service and start receiving agency ad accounts:\n"
    "1. Click on the link below and register to the self-service platform\n"
    "2. Pick a plan for the platform you wish to advertise in. Once clicked, it should redirect to checkout with the free trial activated. "
    "You won't be charged anything yet, only after the trial ends.\n"
    "3. Top up your account balance in \"Wallet\" section with crypto, bank transfer or card payments\n"
    "4. Request new ad accounts through \"Ad Accounts section\" with already loaded balance\n"
    "5. Done! Now just wait for delivery. Under \"Support\" section you should see contacts of your account manager to communicate about account delivery or other issues.\n"
)

FAQ_TEXT = (
    "FAQ: Here is a list of our most common questions answered. please check if it is answered here for convenience:\n"
    "❓Can I add my own card?\n"
    "No, we provide agency credit line accounts, meaning that these accounts will have balances. You can request to top up or clear an account's balance through the self serve dashboard.\n"
    "❓Do you provide business assets along with the ad accounts?\n"
    "Our usual practise is to share the ad accounts with your Business Managers or Profiles. But for Meta, we can provide Business Managers together with the accounts free of charge. You can also buy aged reinstated FB profiles for your ads, reach out to support"
    "❓Service Fee?\n"
    "Service pricing for different platforms might be different, but for Meta it's:\n"
"- 300$ a month service access fee\n"
"- 0-3% ad account top up fee\n"
"We have this pricing in place in order to guarantee the best service possible, and to be able to deliver higher quality agency ad accounts at lower or no top up fees for high spending clients. \n"
"The top up fee is fully refunded if you spend 100k total in 3 months, and you might be eligible for cashback depending on your spend.\n"
    "❓Minimum Top Up?\n"
    "The minimum top up varies for each platform, for Meta it is 250$ to request an account, Google 1000$, TikTok 1000$. "
    "Afterwards, you can top up any amount to the accounts once received.\n"
    "❓What if an ad account gets banned?\n"
    "We can try appealing the account for you, or you, or you can just request to clear out the balance. "
    "We will refund you all the unused balance back in 1-2 business days.\n"
    "❓Can I run BH/GH ads?\n"
    "Yes, but use cloaking & account warmups to avoid bans.\n"
    "❓Can I get a free trial or discount?\n"
    "Yes, with our link you can receive 2 week free trial to test our service free of charge.\n"
)

AURORA_SERVICE_TEXT = (
    "Agency Aurora has been serving ad accounts since 2021, with 350M+ yearly ad spend, 3000+ advertisers, and partnerships with Meta, Google, TikTok, Snapchat and more. \n\n"
    "Aurora aims to serve scaling spenders by providing:\n"
    "🛡️ Whitelisted enterprise accounts: no restrictions, cashback, unlimited scalability\n"
    "📱 All-in-one proprietary technology self serve platform: manage on a single, mobile-friendly platform accessible anywhere.\n"
    "🛠️ Dedicated account management: Strategize with our dedicated account management team.\n\n"
    "Reviews from our customers: https://www.trustpilot.com/review/agency-aurora.com\n"
    "Additionally, Agency Aurora participates in many major worldwide conferences, so if you see us, feel free to check out our booth and have a chat with our expert team right there!"
)

SCHEDULE_TEXT = (
    "To schedule a call, first please register on our platform using the link below. Afterwards, in this chat please send: \n 1. The email you used for registration \n 2. Your platforms of interest \n 3. Approximate daily spend \n We will check your message and send you details about possible call timeslots here in this chat:"
)

SUPPORT_TEXT = (
    "Please write your message and questions here, our team will get in touch with you as fast as possible:\n"
)

ACK_TEXT = (
    "Thanks! Our team will get back to you here shortly. "
    "In the meantime, you can restart the bot and read more about our service using command /start"
)

# ---------- Helper ----------
def build_ticket_header(section: str, user: "telegram.User", message: Message) -> str:
    uname = f"@{user.username}" if user and getattr(user, 'username', None) else f"{user.full_name if user else 'Unknown'}"
    return f"[{section}] From {uname} (id {user.id if user else 'unknown'}):\n{message.text or ''}"

# ---------- Error handler ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update", exc_info=context.error)
    try:
        upd_str = update.to_dict() if isinstance(update, Update) else str(update)
        snippet = json.dumps(upd_str, ensure_ascii=False) if isinstance(upd_str, dict) else upd_str
        logger.error("Update payload: %s", snippet[:1500])
    except Exception:
        pass

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Log /start for analytics
    try:
        if update.effective_user:
            db_save_start(update.effective_user.id)
            # Clear any active support sessions when user restarts
            db_end_support_session(update.effective_user.id)
    except Exception as e:
        logger.error("Failed to log /start: %s", e)

    context.user_data.clear()
    context.user_data["section"] = None
    await update.message.reply_text("Welcome! Choose an option:", reply_markup=main_menu_kb())
    return MAIN_MENU

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    logger.info("/id requested from chat_id=%s type=%s", getattr(chat, "id", None), getattr(chat, "type", None))
    if chat and chat.type == "private":
        await update.message.reply_text(f"Your chat_id is: {chat.id}")
    else:
        await update.message.reply_text("Please DM me /id to receive your private chat_id.")

async def cmd_end_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    
    db_end_support_session(update.effective_user.id)
    context.user_data["section"] = None
    await update.message.reply_text(
        "Support conversation ended. Use /start to access the main menu or click the support options to start a new conversation."
    )

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    logger.info("main_menu callback data=%s", data)

    if data == "main:agency":
        await query.edit_message_text("Agency Ad Account Service — choose an option:", reply_markup=agency_menu_kb())
        return AGENCY_MENU

    if data == "main:cloaking":
        await query.edit_message_text(CLOAKING_TEXT, reply_markup=cloaking_menu_kb())
        return CLOAKING_MENU

    if data == "nav:back:agency":
        try:
            # Delete the current message (works for both text and media messages)
            await query.message.delete()
            
            # Send new agency menu message
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="Agency Ad Account Service — choose an option:",
                reply_markup=agency_menu_kb()
            )
        except Exception as e:
            logger.error(f"Failed to handle back navigation: {e}")
            # Fallback: try normal edit (works for text messages)
            try:
                await query.edit_message_text(
                    "Agency Ad Account Service — choose an option:", 
                    reply_markup=agency_menu_kb()
                )
            except:
                # Last resort: send new message without deleting
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="Agency Ad Account Service — choose an option:",
                    reply_markup=agency_menu_kb()
                )
        return AGENCY_MENU

    await query.edit_message_text("Welcome! Choose an option:", reply_markup=main_menu_kb())
    return MAIN_MENU

async def agency_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    logger.info("agency_router data=%s", data)

    if data == "nav:back:main":
        context.user_data["section"] = None
        # End support session when going back to main
        if update.effective_user:
            db_end_support_session(update.effective_user.id)
        await query.edit_message_text("Welcome! Choose an option:", reply_markup=main_menu_kb())
        return MAIN_MENU

    if data == "nav:back:agency":
        await query.edit_message_text("Agency Ad Account Service — choose an option:", reply_markup=agency_menu_kb())
        return AGENCY_MENU

    if data == "agency:about":
        context.user_data["section"] = None
        await query.edit_message_text(ABOUT_TEXT, reply_markup=back_with_register_kb("agency"))
        return AGENCY_MENU

    if data == "agency:howto":
        context.user_data["section"] = None
        await query.edit_message_text(HOWTO_TEXT, reply_markup=back_with_register_kb("agency"))
        return AGENCY_MENU

    if data == "agency:faq":
        context.user_data["section"] = None
        await query.edit_message_text(FAQ_TEXT, reply_markup=back_with_register_kb("agency"))
        return AGENCY_MENU
    if data == "agency:aurora":
        context.user_data["section"] = None
        try:
            # Send photo from local file with caption and buttons
            with open('aurora-service.jpg', 'rb') as photo:
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=photo,
                    caption=AURORA_SERVICE_TEXT,
                    reply_markup=back_with_register_kb("agency")
                )
            # Delete the original menu message to avoid clutter
            await query.message.delete()
        except Exception as e:
            logger.error(f"Failed to send Aurora service photo: {e}")
            # Fallback to text message if image fails
            await query.edit_message_text(
                AURORA_SERVICE_TEXT,
                reply_markup=back_with_register_kb("agency")
            )
        return AGENCY_MENU

    if data == "agency:schedule":
        context.user_data["section"] = "Schedule a Call"
        if update.effective_user:
            db_start_support_session(update.effective_user.id, "Schedule a Call")
        await query.edit_message_text(SCHEDULE_TEXT, reply_markup=back_with_register_kb("agency"))
        return AGENCY_MENU

    if data == "agency:support":
        context.user_data["section"] = "Talk To Support"
        if update.effective_user:
            db_start_support_session(update.effective_user.id, "Talk To Support")
        await query.edit_message_text(SUPPORT_TEXT, reply_markup=back_with_register_kb("agency"))
        return AGENCY_MENU

    await query.edit_message_text("Agency Ad Account Service — choose an option:", reply_markup=agency_menu_kb())
    return AGENCY_MENU

async def cloaking_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    logger.info("cloaking_router data=%s", data)

    if data == "nav:back:main":
        await query.edit_message_text("Welcome! Choose an option:", reply_markup=main_menu_kb())
        return MAIN_MENU

    await query.edit_message_text(CLOAKING_TEXT, reply_markup=cloaking_menu_kb())
    return CLOAKING_MENU

async def capture_user_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type != "private":
        return

    user = update.effective_user
    if not user:
        return

    # Check both session state and database for persistent support sessions
    session_section = context.user_data.get("section")
    db_section = db_get_support_session(user.id)
    
    section = session_section or db_section
    
    if section not in {"Schedule a Call", "Talk To Support"}:
        return

    msg: Message = update.effective_message
    logger.info("Capture text from user_id=%s section=%s text=%s (session=%s, db=%s)", 
                user.id, section, msg.text, session_section, db_section)

    header = build_ticket_header(section, user, msg)
    header_msg = await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=header)
    logger.info("Posted header to admin chat_id=%s admin_msg_id=%s", ADMIN_CHAT_ID, header_msg.message_id)

    db_save_ticket(user.id, section, header_msg.message_id)
    
    # Update session activity timestamp if this came from DB session
    if db_section:
        conn = db_connect()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE support_sessions SET last_activity = NOW() WHERE user_id = %s;",
                (user.id,)
            )

    await msg.reply_text(ACK_TEXT)

async def cmd_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.id != ADMIN_CHAT_ID:
        logger.info("/reply ignored: wrong chat %s", getattr(update.effective_chat, "id", None))
        return

    msg = update.effective_message
    if not msg or not msg.reply_to_message:
        await msg.reply_text("Please reply to a ticket message with /reply <text>.")
        return

    replied_id = msg.reply_to_message.message_id
    logger.info("/reply in admin chat, replying to admin_msg_id=%s", replied_id)

    ticket = db_get_ticket_by_admin_msg_id(replied_id)
    logger.info("/reply ticket lookup => %s", ticket)
    if not ticket:
        await msg.reply_text("Couldn't find the ticket mapping. Please reply to the original ticket header.")
        return

    args_text = msg.text.removeprefix("/reply").strip() if msg.text else ""
    if not args_text:
        await msg.reply_text("Usage: /reply <message to user>")
        return

    user_id = ticket["user_id"]
    await context.bot.send_message(chat_id=user_id, text=args_text)
    await msg.reply_text(f"Sent to user {user_id}.")

# ---------- Scheduled daily stats ----------
def _yesterday_utc_range():
    # Yesterday in UTC, inclusive start, exclusive end
    today_utc = datetime.now(timezone.utc).date()
    y_start = datetime.combine(today_utc - timedelta(days=1), time.min, tzinfo=timezone.utc)
    y_end = datetime.combine(today_utc, time.min, tzinfo=timezone.utc)
    return y_start, y_end

def db_fetch_daily_stats():
    y_start, y_end = _yesterday_utc_range()
    conn = db_connect()
    with conn.cursor() as cur:
        # Unique starters yesterday
        cur.execute(
            "SELECT COUNT(DISTINCT user_id) FROM starts WHERE started_at >= %s AND started_at < %s;",
            (y_start, y_end),
        )
        starters = cur.fetchone()[0] or 0

        # Total support messages (tickets) yesterday
        cur.execute(
            "SELECT COUNT(*) FROM tickets WHERE created_at >= %s AND created_at < %s;",
            (y_start, y_end),
        )
        total_tickets = cur.fetchone()[0] or 0

        # Split by section
        cur.execute(
            "SELECT section, COUNT(*) FROM tickets WHERE created_at >= %s AND created_at < %s GROUP BY section;",
            (y_start, y_end),
        )
        by_section = dict(cur.fetchall() or [])

    return {
        "start": y_start,
        "end": y_end,
        "starters": starters,
        "total_tickets": total_tickets,
        "tickets_support": by_section.get("Talk To Support", 0),
        "tickets_schedule": by_section.get("Schedule a Call", 0),
    }

async def send_daily_stats(context: ContextTypes.DEFAULT_TYPE):
    try:
        stats = db_fetch_daily_stats()
        window = f"{stats['start'].strftime('%Y-%m-%d')} UTC"
        text = (
            f"Daily Stats for {window}\n"
            f"• Unique /start users: {stats['starters']}\n"
            f"• Support messages (total): {stats['total_tickets']}\n"
            f"   - Talk To Support: {stats['tickets_support']}\n"
            f"   - Schedule a Call: {stats['tickets_schedule']}\n"
        )
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
        logger.info("Daily stats sent to admin.")
    except Exception as e:
        logger.exception("Failed to compute/send daily stats: %s", e)

def schedule_daily_job(app: Application):
    job_queue: JobQueue | None = app.job_queue
    if job_queue is None:
        logger.warning("JobQueue is not available. Install PTB with extras: pip install \"python-telegram-bot[job-queue]\"")
        return
    # Run at 08:00 UTC by default
    run_at = time(hour=8, minute=0, tzinfo=timezone.utc)
    job_queue.run_daily(send_daily_stats, run_at)
    logger.info("Scheduled daily stats job at %s (UTC).", run_at)

# ---------- Main ----------
# ---------- Main ----------
def main() -> None:
    db_init_schema()

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()
    logger.info("Bot starting. ADMIN_CHAT_ID=%s", ADMIN_CHAT_ID)

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [CallbackQueryHandler(main_menu)],
            AGENCY_MENU: [CallbackQueryHandler(agency_router)],
            CLOAKING_MENU: [CallbackQueryHandler(cloaking_router)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("reply", cmd_reply))
    app.add_handler(CommandHandler("endsupport", cmd_end_support))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), capture_user_text))

    app.add_error_handler(error_handler)

    # Schedule the daily recap
    schedule_daily_job(app)

    app.run_polling()

if __name__ == "__main__":
    main()
