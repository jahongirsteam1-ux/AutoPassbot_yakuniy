"""
tgbot.py — Telegram Bot handlerlari.
/start, /login, /accounts, /forwardings, /help komandalar va ConversationHandler.
"""

import logging
import os

from dotenv import load_dotenv
from sqlalchemy import select
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from database import AsyncSessionLocal, Forwarding, User, UserAccount, get_or_create_user
from userbot import decrypt_session, encrypt_session, userbot_manager

load_dotenv()

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MINI_APP_URL = os.getenv("MINI_APP_URL", "")

# ─────────────────────────────────────────────
# ConversationHandler holatlari
# ─────────────────────────────────────────────
PHONE = "PHONE"
CODE = "CODE"
PASSWORD = "PASSWORD"
DONE = "DONE"


# ─────────────────────────────────────────────
# /start komandasi
# ─────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start — Xush kelibsiz xabari va Mini App tugmasi.
    """
    user = update.effective_user

    # Foydalanuvchini DB ga saqlash yoki yangilash
    async with AsyncSessionLocal() as session:
        await get_or_create_user(
            session=session,
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
        )
        await session.commit()

    # Mini App tugmasi
    keyboard = []
    if MINI_APP_URL:
        keyboard.append([
            InlineKeyboardButton(
                "🚀 AutoPass Bot ni ochish",
                web_app=WebAppInfo(url=MINI_APP_URL),
            )
        ])
    keyboard.append([
        InlineKeyboardButton("📱 Akkaunt ulash", callback_data="login"),
        InlineKeyboardButton("📋 Forwardinglar", callback_data="forwardings"),
    ])
    keyboard.append([
        InlineKeyboardButton("❓ Yordam", callback_data="help"),
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_text = (
        f"👋 Salom, <b>{user.first_name}</b>!\n\n"
        "🤖 <b>AutoPass Bot</b> ga xush kelibsiz!\n\n"
        "Bu bot yordamida:\n"
        "✅ Telegram akkauntingizni ulashingiz\n"
        "✅ Istalgan kanaldan istalgan kanalga xabarlarni avtomatik forward qilishingiz\n"
        "✅ Filtrlash va o'zgartirish qoidalarini sozlashingiz mumkin.\n\n"
        "🔽 Pastdagi tugmalardan foydalaning:"
    )

    await update.message.reply_text(
        welcome_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────
# /login komandasi — ConversationHandler
# ─────────────────────────────────────────────
async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    /login — Telefon raqam so'raydi.
    ConversationHandler ning birinchi qadami.
    """
    user = update.effective_user

    # Agar callback query orqali kelsa
    if update.callback_query:
        await update.callback_query.answer()
        message_func = update.callback_query.message.reply_text
    else:
        message_func = update.message.reply_text

    # DB da foydalanuvchini yaratish/tekshirish
    async with AsyncSessionLocal() as session:
        await get_or_create_user(
            session=session,
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
        )
        await session.commit()

    # Kontekstga login ma'lumotlarini saqlash
    context.user_data.clear()

    await message_func(
        "📱 <b>Akkaunt ulash</b>\n\n"
        "Telefon raqamingizni kiriting:\n"
        "<i>Format: +998901234567</i>\n\n"
        "❌ Bekor qilish uchun /cancel yozing",
        parse_mode=ParseMode.HTML,
    )
    return PHONE


async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    Telefon raqamni qabul qiladi va SMS kod yuboradi.
    """
    phone = update.message.text.strip()

    # Telefon raqam formati tekshirish
    if not phone.startswith("+") or len(phone) < 10:
        await update.message.reply_text(
            "❌ Noto'g'ri format. Iltimos, quyidagi formatda kiriting:\n"
            "<i>+998901234567</i>",
            parse_mode=ParseMode.HTML,
        )
        return PHONE

    await update.message.reply_text(
        f"⏳ {phone} raqamiga kod yuborilmoqda..."
    )

    try:
        # Telethon orqali SMS kod yuborish
        phone_code_hash, _ = await userbot_manager.send_code(phone)

        # Telefon va code hash ni saqlab qo'yish
        context.user_data["phone"] = phone
        context.user_data["phone_code_hash"] = phone_code_hash

        await update.message.reply_text(
            "✅ Kod yuborildi!\n\n"
            "📩 Telegramdan kelgan <b>5 xonali kodni</b> kiriting:\n"
            "<i>Masalan: 12345</i>\n\n"
            "❌ Bekor qilish uchun /cancel yozing",
            parse_mode=ParseMode.HTML,
        )
        return CODE

    except Exception as e:
        logger.error(f"Kod yuborishda xato: {e}")
        await update.message.reply_text(
            f"❌ Xato yuz berdi: <code>{str(e)[:200]}</code>\n\n"
            "Qaytadan urinish uchun /login yozing.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END


async def login_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    SMS kodni qabul qiladi va tizimga kiradi.
    2FA kerak bo'lsa parol so'raydi.
    """
    code = update.message.text.strip()
    phone = context.user_data.get("phone")

    if not phone:
        await update.message.reply_text("❌ Xato yuz berdi. /login qaytadan bosing.")
        return ConversationHandler.END

    # Kodni faqat raqamlardan iborat bo'lishini tekshirish
    clean_code = code.replace(" ", "").replace("-", "")
    if not clean_code.isdigit():
        await update.message.reply_text(
            "❌ Kod faqat raqamlardan iborat bo'lishi kerak.\n"
            "Qaytadan kiriting:"
        )
        return CODE

    try:
        session_string, needs_2fa = await userbot_manager.sign_in(
            phone=phone,
            code=clean_code,
        )

        if needs_2fa:
            # 2FA kerak
            context.user_data["needs_2fa"] = True
            await update.message.reply_text(
                "🔐 <b>Ikki bosqichli tekshiruv (2FA)</b>\n\n"
                "Telegram parolingizni kiriting:\n\n"
                "❌ Bekor qilish uchun /cancel yozing",
                parse_mode=ParseMode.HTML,
            )
            return PASSWORD

        # Muvaffaqiyatli kirdi — session ni DB ga saqlash
        await _save_session(
            update=update,
            context=context,
            phone=phone,
            session_string=session_string,
        )
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Sign in xato: {e}")
        error_msg = str(e)
        if "PhoneCodeInvalid" in error_msg or "PHONE_CODE_INVALID" in error_msg:
            await update.message.reply_text(
                "❌ Noto'g'ri kod. Qaytadan kiriting:"
            )
            return CODE
        elif "PhoneCodeExpired" in error_msg or "PHONE_CODE_EXPIRED" in error_msg:
            await update.message.reply_text(
                "❌ Kod muddati o'tgan. /login qaytadan bosing."
            )
            return ConversationHandler.END
        else:
            await update.message.reply_text(
                f"❌ Xato: <code>{error_msg[:200]}</code>\n\n"
                "/login qaytadan bosing.",
                parse_mode=ParseMode.HTML,
            )
            return ConversationHandler.END


async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    2FA parolni qabul qiladi va tizimga kiradi.
    """
    password = update.message.text.strip()
    phone = context.user_data.get("phone")

    if not phone:
        await update.message.reply_text("❌ Xato yuz berdi. /login qaytadan bosing.")
        return ConversationHandler.END

    try:
        session_string = await userbot_manager.sign_in_2fa(
            phone=phone,
            password=password,
        )

        # Muvaffaqiyatli kirdi — session ni DB ga saqlash
        await _save_session(
            update=update,
            context=context,
            phone=phone,
            session_string=session_string,
        )
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"2FA xato: {e}")
        error_msg = str(e)
        if "PasswordHashInvalid" in error_msg or "PASSWORD_HASH_INVALID" in error_msg:
            await update.message.reply_text(
                "❌ Noto'g'ri parol. Qaytadan kiriting:"
            )
            return PASSWORD
        else:
            await update.message.reply_text(
                f"❌ Xato: <code>{error_msg[:200]}</code>\n\n"
                "/login qaytadan bosing.",
                parse_mode=ParseMode.HTML,
            )
            return ConversationHandler.END


async def _save_session(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    phone: str,
    session_string: str,
) -> None:
    """
    Session stringni shifrlaydi va DB ga saqlaydi.
    Telethon clientni ham RAM ga qo'shadi.
    """
    user = update.effective_user

    try:
        # Session stringni shifrlash
        encrypted = encrypt_session(session_string)

        async with AsyncSessionLocal() as session:
            # Foydalanuvchini topish
            result = await session.execute(
                select(User).where(User.telegram_id == user.id)
            )
            db_user = result.scalar_one_or_none()

            if not db_user:
                await update.message.reply_text("❌ Foydalanuvchi topilmadi. /start bosing.")
                return

            # Xuddi shu telefon raqam bilan akkaunt bormi tekshirish
            result2 = await session.execute(
                select(UserAccount).where(
                    UserAccount.user_id == db_user.id,
                    UserAccount.phone == phone,
                )
            )
            existing_account = result2.scalar_one_or_none()

            if existing_account:
                # Mavjud akkauntni yangilash
                existing_account.session_string = encrypted
                existing_account.is_active = True
                account_id = existing_account.id
            else:
                # Yangi akkaunt yaratish
                new_account = UserAccount(
                    user_id=db_user.id,
                    phone=phone,
                    session_string=encrypted,
                    is_active=True,
                )
                session.add(new_account)
                await session.flush()
                account_id = new_account.id

            await session.commit()

        # Telethon clientni RAM ga qo'shish
        success = await userbot_manager.add_client(
            account_id=account_id,
            user_id=db_user.id,
            session_string=session_string,
            forwardings=[],
        )

        if success:
            await update.message.reply_text(
                f"🎉 <b>Muvaffaqiyatli ulandi!</b>\n\n"
                f"📱 Telefon: <code>{phone}</code>\n\n"
                "Endi forwardinglar yaratishingiz mumkin.\n"
                "/forwardings — forwardinglar ro'yxati",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                "⚠️ Akkaunt saqlandi, lekin ulanishda muammo bo'ldi.\n"
                "Qaytadan /start bosing."
            )

    except Exception as e:
        logger.error(f"Session saqlashda xato: {e}")
        await update.message.reply_text(
            f"❌ Saqlashda xato: <code>{str(e)[:200]}</code>",
            parse_mode=ParseMode.HTML,
        )


async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Login jarayonini bekor qilish."""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Login bekor qilindi.\n/start — bosh menu"
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────
# /accounts komandasi
# ─────────────────────────────────────────────
async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /accounts — Ulangan akkauntlar ro'yxati.
    """
    user = update.effective_user

    async with AsyncSessionLocal() as session:
        # Foydalanuvchini topish
        result = await session.execute(
            select(User).where(User.telegram_id == user.id)
        )
        db_user = result.scalar_one_or_none()

        if not db_user:
            await update.message.reply_text(
                "❌ Avval /start bosing."
            )
            return

        # Akkauntlarni olish
        result2 = await session.execute(
            select(UserAccount).where(UserAccount.user_id == db_user.id)
        )
        accounts = result2.scalars().all()

    if not accounts:
        keyboard = [[InlineKeyboardButton("📱 Akkaunt ulash", callback_data="login")]]
        await update.message.reply_text(
            "📋 Ulangan akkauntlar yo'q.\n\nAkkaunt ulash uchun tugmani bosing:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    text = "📋 <b>Ulangan akkauntlar:</b>\n\n"
    keyboard = []

    for acc in accounts:
        status_emoji = "✅" if acc.is_active else "❌"
        connected = "🟢 Ulangan" if acc.id in userbot_manager.clients else "🔴 Ulanmagan"
        text += f"{status_emoji} <code>{acc.phone}</code> — {connected}\n"
        keyboard.append([
            InlineKeyboardButton(
                f"🗑 {acc.phone} o'chirish",
                callback_data=f"delete_account:{acc.id}",
            )
        ])

    keyboard.append([InlineKeyboardButton("➕ Yangi akkaunt", callback_data="login")])
    keyboard.append([InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")])

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────
# /forwardings komandasi
# ─────────────────────────────────────────────
async def forwardings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /forwardings — Aktiv forwardinglar ro'yxati.
    """
    user = update.effective_user

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user.id)
        )
        db_user = result.scalar_one_or_none()

        if not db_user:
            await update.message.reply_text("❌ Avval /start bosing.")
            return

        result2 = await session.execute(
            select(Forwarding).where(Forwarding.user_id == db_user.id)
        )
        forwardings = result2.scalars().all()

    if not forwardings:
        await update.message.reply_text(
            "📋 Forwardinglar yo'q.\n\n"
            "Mini App orqali yangi forwarding yaratishingiz mumkin.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🚀 Mini App ochish",
                    web_app=WebAppInfo(url=MINI_APP_URL) if MINI_APP_URL else None,
                )
            ]]) if MINI_APP_URL else None,
        )
        return

    text = "📋 <b>Forwardinglar:</b>\n\n"
    keyboard = []

    for fwd in forwardings:
        status = "✅ Aktiv" if fwd.is_active else "⏸ To'xtatilgan"
        mode = "📋 Copy" if fwd.copy_mode else "↪️ Forward"
        text += f"<b>{fwd.name}</b>\n"
        text += f"  └ {status} | {mode}\n"

        # Toggle va o'chirish tugmalari
        toggle_label = "⏸ To'xtatish" if fwd.is_active else "▶️ Yoqish"
        keyboard.append([
            InlineKeyboardButton(toggle_label, callback_data=f"toggle_fwd:{fwd.id}"),
            InlineKeyboardButton("🗑 O'chirish", callback_data=f"delete_fwd:{fwd.id}"),
        ])

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────
# /help komandasi
# ─────────────────────────────────────────────
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /help — Bot haqida yordam xabari.
    """
    help_text = (
        "❓ <b>AutoPass Bot yordam</b>\n\n"
        "<b>Komandalar:</b>\n"
        "/start — Bosh menu\n"
        "/login — Telegram akkaunt ulash\n"
        "/accounts — Ulangan akkauntlar\n"
        "/forwardings — Forwardinglar ro'yxati\n"
        "/help — Yordam\n\n"
        "<b>Qanday ishlaydi?</b>\n"
        "1️⃣ /login orqali Telegram akkauntingizni ulang\n"
        "2️⃣ Mini App orqali forwarding yarating\n"
        "3️⃣ Manba va manzil kanallarni tanlang\n"
        "4️⃣ Filtrlash va o'zgartirish qoidalarini sozlang\n"
        "5️⃣ Tayyor! Xabarlar avtomatik forward qilinadi\n\n"
        "<b>Muhim:</b>\n"
        "• Private kanaldan o'qish uchun akkauntingiz a'zo bo'lishi kerak\n"
        "• Manzil kanalga bot admin bo'lishi kerak\n"
        "• Copy mode — muallifni yashiradi\n"
        "• Forward mode — muallifni ko'rsatadi"
    )

    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────
# Callback query handler (inline button bosilganda)
# ─────────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Barcha inline tugma bosilishlarini boshqaradi.
    """
    query = update.callback_query
    await query.answer()

    data = query.data

    # ── Login ────────────────────────────────
    if data == "login":
        # ConversationHandler ni ishga tushirish uchun /login yuborish
        await query.message.reply_text(
            "📱 <b>Akkaunt ulash</b>\n\n"
            "Telefon raqamingizni kiriting:\n"
            "<i>Format: +998901234567</i>\n\n"
            "❌ Bekor qilish uchun /cancel yozing",
            parse_mode=ParseMode.HTML,
        )
        context.user_data["awaiting_phone"] = True

    # ── Akkaunt o'chirish ────────────────────
    elif data.startswith("delete_account:"):
        account_id = int(data.split(":")[1])
        await _delete_account(query, account_id)

    # ── Forwarding toggle ────────────────────
    elif data.startswith("toggle_fwd:"):
        fwd_id = int(data.split(":")[1])
        await _toggle_forwarding(query, fwd_id)

    # ── Forwarding o'chirish ─────────────────
    elif data.startswith("delete_fwd:"):
        fwd_id = int(data.split(":")[1])
        await _delete_forwarding(query, fwd_id)

    # ── Orqaga ──────────────────────────────
    elif data == "back_to_start":
        await query.message.reply_text(
            "Bosh menudan boshlang: /start"
        )

    # ── Yordam ──────────────────────────────
    elif data == "help":
        await help_command.__wrapped__(update, context) if hasattr(help_command, "__wrapped__") else None
        await query.message.reply_text(
            "❓ Yordam uchun /help yozing."
        )

    # ── Forwardinglar ────────────────────────
    elif data == "forwardings":
        await query.message.reply_text(
            "Forwardinglar uchun /forwardings yozing."
        )


async def _delete_account(query, account_id: int) -> None:
    """Akkauntni o'chiradi."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(UserAccount).where(UserAccount.id == account_id)
            )
            account = result.scalar_one_or_none()

            if not account:
                await query.message.reply_text("❌ Akkaunt topilmadi.")
                return

            phone = account.phone
            account.is_active = False  # Soft delete
            await session.commit()

        # Telethon clientni ham o'chirish
        await userbot_manager.remove_client(account_id)

        await query.message.reply_text(
            f"✅ Akkaunt <code>{phone}</code> o'chirildi.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Akkaunt o'chirishda xato: {e}")
        await query.message.reply_text("❌ Xato yuz berdi.")


async def _toggle_forwarding(query, fwd_id: int) -> None:
    """Forwarding holati yoq/o'chir."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Forwarding).where(Forwarding.id == fwd_id)
            )
            fwd = result.scalar_one_or_none()

            if not fwd:
                await query.message.reply_text("❌ Forwarding topilmadi.")
                return

            fwd.is_active = not fwd.is_active
            status = "✅ Yoqildi" if fwd.is_active else "⏸ To'xtatildi"
            name = fwd.name
            await session.commit()

        await query.message.reply_text(
            f"{status}: <b>{name}</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Toggle xato: {e}")
        await query.message.reply_text("❌ Xato yuz berdi.")


async def _delete_forwarding(query, fwd_id: int) -> None:
    """Forwardingi o'chiradi."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Forwarding).where(Forwarding.id == fwd_id)
            )
            fwd = result.scalar_one_or_none()

            if not fwd:
                await query.message.reply_text("❌ Forwarding topilmadi.")
                return

            name = fwd.name
            await session.delete(fwd)
            await session.commit()

        await query.message.reply_text(
            f"✅ Forwarding o'chirildi: <b>{name}</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Forwarding o'chirishda xato: {e}")
        await query.message.reply_text("❌ Xato yuz berdi.")


# ─────────────────────────────────────────────
# Bot Application yaratish
# ─────────────────────────────────────────────
def create_bot_application() -> Application:
    """
    Telegram bot applicationni yaratadi va barcha handlerlarni ro'yxatga oladi.
    
    Returns:
        Tayyor Application instance
    """
    app = Application.builder().token(BOT_TOKEN).build()

    # ── ConversationHandler (Login) ───────────
    login_conv = ConversationHandler(
        entry_points=[
            CommandHandler("login", login_start),
        ],
        states={
            PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone),
            ],
            CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_code),
            ],
            PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_password),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", login_cancel),
            CommandHandler("start", login_cancel),
        ],
        per_user=True,
        per_chat=True,
    )

    # ── Handlerlarni qo'shish ─────────────────
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(login_conv)
    app.add_handler(CommandHandler("accounts", accounts_command))
    app.add_handler(CommandHandler("forwardings", forwardings_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("✅ Bot handlerlari ro'yxatga olindi.")
    return app
