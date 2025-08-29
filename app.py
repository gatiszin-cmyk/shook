import os
from dotenv import load_dotenv
from urllib.parse import urlparse  # DB: parse DATABASE_URL
import psycopg2  # DB driver

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Message
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Load env from runtime.env (as requested)
load_dotenv(dotenv_path="runtime.env")
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Put it in runtime.env as BOT_TOKEN=...")

# DB: connect once and init schema
DATABASE_URL = os.getenv("DATABASE_URL")  # set this in Railway Variables
_db_conn = None  # global connection handle

def db_connect():
    global _db_conn
    if _db_conn is not None:
        return _db_conn
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing. Add it in Railway service Variables.")
    p = urlparse(DATABASE_URL)  # parses postgres://user:pass@host:port/db [15]
    _db_conn = psycopg2.connect(
        dbname=p.path.lstrip("/"),
        user=p.username,
        password=p.password,
        host=p.hostname,
        port=p.port,
        sslmode="require",  # typical for managed Postgres
    )
    _db_conn.autocommit = True
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

def db_save_ticket(user_id: int, section: str, admin_msg_id: int) -> int:
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tickets (user_id, section, admin_msg_id) VALUES (%s, %s, %s) RETURNING ticket_id;",
            (user_id, section, admin_msg_id),
        )
        row = cur.fetchone()      # returns a tuple like (ticket_id,)
        return row if row else None  # [6][14]

def db_get_ticket_by_admin_msg_id(admin_msg_id: int):
    conn = db_connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ticket_id, user_id, section FROM tickets WHERE admin_msg_id=%s;",
            (admin_msg_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        # row = (ticket_id, user_id, section)
        return {"ticket_id": row, "user_id": row[16], "section": row[17]}  # [6]

# States
MAIN_MENU, AGENCY_MENU, CLOAKING_MENU = range(3)

# External links
CLOAKING_URL = "https://socialhook.media/sp/cloaking-course/?utm_source=telegram"
REGISTER_URL = "https://vantage.agency-aurora.com/?ref=SOCIALHOOK"

# Admin destination (private chat with you)
ADMIN_CHAT_ID = 8088620127  # provided

# --- Keyboards ---------------------------------------------------------------

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
        [InlineKeyboardButton("📅📞 Schedule a Call", callback_data="agency:schedule")],
        [InlineKeyboardButton("💬🤝 Talk To Support", callback_data="agency:support")],
        [InlineKeyboardButton("⬅️ Back", callback_data="nav:back:main")],
    ])

def cloaking_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Get Cloaking Mastery Now!", url=CLOAKING_URL)],
        [InlineKeyboardButton("⬅️ Back", callback_data="nav:back:main")],
    ])

def back_with_register_kb(back_target: str = "agency") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔗 Register Now", url=REGISTER_URL)],
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

# --- Content -----------------------------------------------------------------

CLOAKING_TEXT = (
    "🔥 Socialhook Cloaking Mastery Course is LIVE!\n"
    "Learn how to run unrestricted ads on Meta & Google!\n"
    "What you’ll get:\n"
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

SCHEDULE_TEXT = (
    "To schedule a call, first please register on our platform using the link below. Afterwards, in this chat please send: \n 1. The email you used for registration \n 2. Your platforms of interest \n 3. Approximate daily spend \n We will check your message and send you details about possible call timeslots here in this chat:"
)

SUPPORT_TEXT = (
    "Please write your message and questions here, our team will get in touch with you as fast as possible:\n"
)

# --- Helper: build a ticket header for admin -------------------------------

def build_ticket_header(section: str, user: "telegram.User", message: Message) -> str:
    uname = f"@{user.username}" if user and user.username else f"{user.full_name if user else 'Unknown'}"
    return (
        f"[{section}] From {uname} (id {user.id if user else 'unknown'}):\n"
        f"{message.text or ''}"
    )

# --- Handlers ----------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["section"] = None  # reset section flag
    await update.message.reply_text(
        "Welcome! Choose an option:",
        reply_markup=main_menu_kb()
    )
    return MAIN_MENU

# Temporary command to return the private chat_id where it's invoked
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat and chat.type == "private":
        await update.message.reply_text(f"Your chat_id is: {chat.id}")
    else:
        await update.message.reply_text("Please DM me /id to receive your private chat_id.")

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "main:agency":
        await query.edit_message_text(
            "Agency Ad Account Service — choose an option:",
            reply_markup=agency_menu_kb()
        )
        return AGENCY_MENU

    if data == "main:cloaking":
        await query.edit_message_text(
            CLOAKING_TEXT,
            reply_markup=cloaking_menu_kb()
        )
        return CLOAKING_MENU

    if data == "nav:back:main":
        await query.edit_message_text(
            "Welcome! Choose an option:",
            reply_markup=main_menu_kb()
        )
        return MAIN_MENU

    # Fallback to main menu
    await query.edit_message_text(
        "Welcome! Choose an option:",
        reply_markup=main_menu_kb()
    )
    return MAIN_MENU

async def agency_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    # Back actions
    if data == "nav:back:main":
        context.user_data["section"] = None
        await query.edit_message_text(
            "Welcome! Choose an option:",
            reply_markup=main_menu_kb()
        )
        return MAIN_MENU

    if data == "nav:back:agency":
        await query.edit_message_text(
            "Agency Ad Account Service — choose an option:",
            reply_markup=agency_menu_kb()
        )
        return AGENCY_MENU

    # Agency submenu items
    if data == "agency:about":
        context.user_data["section"] = None  # no free-text capture here
        await query.edit_message_text(
            ABOUT_TEXT,
            reply_markup=back_with_register_kb("agency")
        )
        return AGENCY_MENU

    if data == "agency:howto":
        context.user_data["section"] = None
        await query.edit_message_text(
            HOWTO_TEXT,
            reply_markup=back_with_register_kb("agency")
        )
        return AGENCY_MENU

    if data == "agency:faq":
        context.user_data["section"] = None
        await query.edit_message_text(
            FAQ_TEXT,
            reply_markup=back_with_register_kb("agency")
        )
        return AGENCY_MENU

    if data == "agency:schedule":
        # Enable capturing subsequent user messages for "Schedule a Call"
        context.user_data["section"] = "Schedule a Call"
        await query.edit_message_text(
            SCHEDULE_TEXT,
            reply_markup=back_with_register_kb("agency")
        )
        return AGENCY_MENU

    if data == "agency:support":
        # Enable capturing subsequent user messages for "Talk To Support"
        context.user_data["section"] = "Talk To Support"
        await query.edit_message_text(
            SUPPORT_TEXT,
            reply_markup=back_with_register_kb("agency")
        )
        return AGENCY_MENU

    # Default: show agency menu
    await query.edit_message_text(
        "Agency Ad Account Service — choose an option:",
        reply_markup=agency_menu_kb()
    )
    return AGENCY_MENU

async def cloaking_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "nav:back:main":
        await query.edit_message_text(
            "Welcome! Choose an option:",
            reply_markup=main_menu_kb()
        )
        return MAIN_MENU

    # Default stay on cloaking info
    await query.edit_message_text(
        CLOAKING_TEXT,
        reply_markup=cloaking_menu_kb()
    )
    return CLOAKING_MENU

# --- Capture user free text when in Schedule/Support -------------------------

ACK_TEXT = (
    "Thanks! Our team will get back to you here shortly. "
    "In the meantime, you can restart the bot and read more about our service using command /start"
)

async def capture_user_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Trigger only in private chats with the bot
    if not update.effective_chat or update.effective_chat.type != "private":
        return

    section = context.user_data.get("section")
    if section not in {"Schedule a Call", "Talk To Support"}:
        # Ignore texts outside these sections
        return

    user = update.effective_user
    msg: Message = update.effective_message

    # Build and send a ticket message to admin chat, copying user's text
    header = build_ticket_header(section, user, msg)
    header_msg = await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=header)

    # DB: persist the mapping so admin replies work after restarts
    db_save_ticket(user.id, section, header_msg.message_id)

    # Acknowledge to user (updated message)
    await msg.reply_text(ACK_TEXT)  # [4][8]

# --- Admin replies using /reply command in admin chat ------------------------

async def cmd_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage: Admin replies in ADMIN_CHAT_ID by replying to the ticket header message,
    with a command like: /reply Hello there, thanks for reaching out!
    The bot will send that text back to the original user.
    """
    # Ensure this is in the admin chat
    if not update.effective_chat or update.effective_chat.id != ADMIN_CHAT_ID:
        return

    msg = update.effective_message
    if not msg or not msg.reply_to_message:
        await msg.reply_text("Please reply to a ticket message with /reply <text>.")
        return

    # Identify which ticket this reply refers to (DB lookup instead of in-memory)
    replied_id = msg.reply_to_message.message_id
    ticket = db_get_ticket_by_admin_msg_id(replied_id)
    if not ticket:
        await msg.reply_text("Couldn't find the ticket mapping. Please reply to the original ticket header.")
        return

    # Extract the reply text after the command
    args_text = msg.text.removeprefix("/reply").strip() if msg.text else ""
    if not args_text:
        await msg.reply_text("Usage: /reply <message to user>")
        return

    user_id = ticket["user_id"]

    # Send the admin's reply to the original user
    await context.bot.send_message(chat_id=user_id, text=args_text)

    # Optional confirmation to admin
    await msg.reply_text(f"Sent to user {user_id}.")

def main() -> None:
    # DB: ensure schema is ready on startup
    if DATABASE_URL:
        db_init_schema()  # creates tickets table and index if not present

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [CallbackQueryHandler(main_menu)],
            AGENCY_MENU: [CallbackQueryHandler(agency_router)],
            CLOAKING_MENU: [CallbackQueryHandler(cloaking_router)],
        },
        fallbacks=[
            CommandHandler("start", start),
        ],
        allow_reentry=True,
    )

    # Register the /id and /reply commands
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("reply", cmd_reply))

    # Capture any text from users while in Schedule/Support sections
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), capture_user_text))

    app.add_handler(conv)
    app.run_polling()

if __name__ == "__main__":
    main()
