"""
server.py — FastAPI web server.
Telegram webhook, API endpointlar va Mini App serve qilish.
"""

import hashlib
import hmac
import json
import logging
import os
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Bot, Update

from database import (
    AsyncSessionLocal,
    Forwarding,
    ForwardLog,
    User,
    UserAccount,
    get_db,
    get_or_create_user,
    init_db,
)
from tgbot import create_bot_application
from userbot import decrypt_session, encrypt_session, userbot_manager

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
MINI_APP_URL = os.getenv("MINI_APP_URL", "")

# index.html fayli joylashgan yo'l
BASE_DIR = Path(__file__).parent
INDEX_HTML = BASE_DIR / "index.html"

# Bot application (global)
bot_app = None


# ─────────────────────────────────────────────
# Lifespan (startup / shutdown)
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Server ishga tushganda va to'xtaganda bajariladi.
    """
    global bot_app

    logger.info("🚀 Server ishga tushmoqda...")

    # 1. DB ni ishga tushirish
    await init_db()

    # 2. Bot applicationni yaratish
    bot_app = create_bot_application()
    await bot_app.initialize()
    await bot_app.start()

    # 3. Telegram webhook o'rnatish
    if WEBHOOK_URL and BOT_TOKEN:
        webhook_endpoint = f"{WEBHOOK_URL}/webhook"
        try:
            bot = Bot(token=BOT_TOKEN)
            await bot.set_webhook(
                url=webhook_endpoint,
                allowed_updates=Update.ALL_TYPES,
            )
            logger.info(f"✅ Webhook o'rnatildi: {webhook_endpoint}")
        except Exception as e:
            logger.error(f"❌ Webhook o'rnatishda xato: {e}")

    # 4. DB dagi barcha aktiv sessionlarni yuklash
    await userbot_manager.load_all_sessions()

    logger.info("✅ Bot va UserBot ishga tushdi!")

    yield  # Server ishlamoqda

    # Shutdown
    logger.info("🛑 Server to'xtatilmoqda...")
    if bot_app:
        await bot_app.stop()
        await bot_app.shutdown()

    # Barcha Telethon clientlarni to'xtatish
    for account_id in list(userbot_manager.clients.keys()):
        await userbot_manager.remove_client(account_id)

    logger.info("👋 Server to'xtatildi.")


# ─────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────
app = FastAPI(
    title="AutoPass Bot",
    description="Telegram kanal forwarder — Junction Bot kloni",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS (Mini App uchun)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────
# Pydantic modellari (request/response)
# ─────────────────────────────────────────────
class CreateForwardingRequest(BaseModel):
    telegram_id: int
    account_id: int
    name: str
    source_identifier: str  # username, link yoki chat_id
    dest_identifier: str
    copy_mode: bool = True
    remove_caption: bool = False
    filters: dict = {}
    modifications: dict = {}
    schedule: dict = {}


class ToggleForwardingRequest(BaseModel):
    is_active: bool


class ValidateInitDataRequest(BaseModel):
    init_data: str


# ─────────────────────────────────────────────
# Mini App initData tekshirish (xavfsizlik)
# ─────────────────────────────────────────────
def verify_telegram_init_data(init_data: str) -> Optional[dict]:
    """
    Telegram Mini App initData ni HMAC-SHA256 bilan tekshiradi.
    
    Returns:
        Tekshirilgan user ma'lumotlari yoki None (xato bo'lsa)
    """
    try:
        # initData ni parse qilish
        parsed = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
        hash_value = parsed.pop("hash", None)

        if not hash_value:
            return None

        # Data check string yaratish
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )

        # Secret key (BOT_TOKEN dan)
        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256,
        ).digest()

        # HMAC hisoblash
        expected_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected_hash, hash_value):
            return None

        # User ma'lumotlarini qaytarish
        user_data = json.loads(parsed.get("user", "{}"))
        return user_data

    except Exception as e:
        logger.error(f"initData tekshirishda xato: {e}")
        return None


# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@app.get("/")
async def root():
    """Server sog'ligini tekshirish."""
    return {"status": "ok", "service": "AutoPass Bot"}


# ── Webhook ──────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request):
    """
    Telegram webhook — botga kelgan yangi xabarlarni qabul qiladi.
    """
    if not bot_app:
        raise HTTPException(status_code=503, detail="Bot hali tayyor emas")

    try:
        body = await request.json()
        update = Update.de_json(body, bot_app.bot)
        await bot_app.process_update(update)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"Webhook xato: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


# ── Mini App (index.html) ────────────────────
@app.get("/app")
async def serve_mini_app():
    """
    Telegram Mini App sahifasini qaytaradi.
    """
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=404, detail="index.html topilmadi")
    return FileResponse(str(INDEX_HTML), media_type="text/html")


# ── API: Foydalanuvchi ma'lumotlari ─────────
@app.get("/api/user/{telegram_id}")
async def get_user(telegram_id: int, db: AsyncSession = Depends(get_db)):
    """Foydalanuvchi ma'lumotlarini qaytaradi."""
    result = await db.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="Foydalanuvchi topilmadi")

    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "first_name": user.first_name,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
    }


# ── API: Ulangan akkauntlar ──────────────────
@app.get("/api/accounts/{telegram_id}")
async def get_accounts(telegram_id: int, db: AsyncSession = Depends(get_db)):
    """Foydalanuvchining ulangan akkauntlarini qaytaradi."""
    # Foydalanuvchini topish
    user_result = await db.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = user_result.scalar_one_or_none()

    if not user:
        return {"accounts": []}

    # Akkauntlarni olish
    result = await db.execute(
        select(UserAccount).where(
            UserAccount.user_id == user.id,
            UserAccount.is_active == True,
        )
    )
    accounts = result.scalars().all()

    return {
        "accounts": [
            {
                "id": acc.id,
                "phone": acc.phone,
                "is_active": acc.is_active,
                "is_connected": acc.id in userbot_manager.clients,
                "created_at": acc.created_at.isoformat(),
            }
            for acc in accounts
        ]
    }


# ── API: Forwardinglar ro'yxati ──────────────
@app.get("/api/forwardings/{telegram_id}")
async def get_forwardings(telegram_id: int, db: AsyncSession = Depends(get_db)):
    """Foydalanuvchining forwardinglarini qaytaradi."""
    user_result = await db.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = user_result.scalar_one_or_none()

    if not user:
        return {"forwardings": []}

    result = await db.execute(
        select(Forwarding).where(Forwarding.user_id == user.id)
        .order_by(desc(Forwarding.created_at))
    )
    forwardings = result.scalars().all()

    return {
        "forwardings": [
            {
                "id": fwd.id,
                "name": fwd.name,
                "account_id": fwd.account_id,
                "source_chat_id": fwd.source_chat_id,
                "source_username": fwd.source_username,
                "dest_chat_id": fwd.dest_chat_id,
                "dest_username": fwd.dest_username,
                "is_active": fwd.is_active,
                "copy_mode": fwd.copy_mode,
                "remove_caption": fwd.remove_caption,
                "filters": fwd.filters,
                "modifications": fwd.modifications,
                "schedule": fwd.schedule,
                "created_at": fwd.created_at.isoformat(),
            }
            for fwd in forwardings
        ]
    }


# ── API: Yangi forwarding yaratish ───────────
@app.post("/api/forwarding/create")
async def create_forwarding(
    body: CreateForwardingRequest,
    db: AsyncSession = Depends(get_db),
):
    """Yangi forwarding qoidasi yaratadi."""

    # Foydalanuvchini topish
    user_result = await db.execute(
        select(User).where(User.telegram_id == body.telegram_id)
    )
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Foydalanuvchi topilmadi")

    # Akkauntni tekshirish
    acc_result = await db.execute(
        select(UserAccount).where(
            UserAccount.id == body.account_id,
            UserAccount.user_id == user.id,
            UserAccount.is_active == True,
        )
    )
    account = acc_result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Akkaunt topilmadi")

    # Chat ma'lumotlarini olish (Telethon orqali)
    source_chat_id = None
    source_username = None
    dest_chat_id = None
    dest_username = None

    if body.account_id in userbot_manager.clients:
        # Manba chat
        source_info = await userbot_manager.get_chat_info(
            body.account_id, body.source_identifier
        )
        if source_info:
            source_chat_id, source_username = source_info

        # Manzil chat
        dest_info = await userbot_manager.get_chat_info(
            body.account_id, body.dest_identifier
        )
        if dest_info:
            dest_chat_id, dest_username = dest_info
    else:
        # Telethon ulanmagan — faqat username saqlash
        source_username = body.source_identifier
        dest_username = body.dest_identifier

    # Forwarding yaratish
    new_fwd = Forwarding(
        user_id=user.id,
        account_id=body.account_id,
        name=body.name,
        source_chat_id=source_chat_id,
        source_username=source_username,
        dest_chat_id=dest_chat_id,
        dest_username=dest_username,
        copy_mode=body.copy_mode,
        remove_caption=body.remove_caption,
        filters=body.filters,
        modifications=body.modifications,
        schedule=body.schedule,
    )
    db.add(new_fwd)
    await db.flush()
    fwd_id = new_fwd.id

    # Telethon clientni yangilash
    await userbot_manager.refresh_forwardings(body.account_id)

    return {
        "success": True,
        "forwarding_id": fwd_id,
        "source_chat_id": source_chat_id,
        "dest_chat_id": dest_chat_id,
    }


# ── API: Forwarding yoq/o'chir ───────────────
@app.put("/api/forwarding/{fwd_id}/toggle")
async def toggle_forwarding(
    fwd_id: int,
    body: ToggleForwardingRequest,
    db: AsyncSession = Depends(get_db),
):
    """Forwardingi yoqadi yoki o'chiradi."""
    result = await db.execute(
        select(Forwarding).where(Forwarding.id == fwd_id)
    )
    fwd = result.scalar_one_or_none()

    if not fwd:
        raise HTTPException(status_code=404, detail="Forwarding topilmadi")

    fwd.is_active = body.is_active
    await db.flush()

    return {"success": True, "is_active": fwd.is_active}


# ── API: Forwarding o'chirish ────────────────
@app.delete("/api/forwarding/{fwd_id}")
async def delete_forwarding(
    fwd_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Forwardingi o'chiradi."""
    result = await db.execute(
        select(Forwarding).where(Forwarding.id == fwd_id)
    )
    fwd = result.scalar_one_or_none()

    if not fwd:
        raise HTTPException(status_code=404, detail="Forwarding topilmadi")

    await db.delete(fwd)

    return {"success": True}


# ── API: Loglar ──────────────────────────────
@app.get("/api/logs/{telegram_id}")
async def get_logs(telegram_id: int, db: AsyncSession = Depends(get_db)):
    """Foydalanuvchining oxirgi 50 ta forward logini qaytaradi."""
    user_result = await db.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = user_result.scalar_one_or_none()

    if not user:
        return {"logs": []}

    # Foydalanuvchining barcha forwarding IDlarini olish
    fwd_result = await db.execute(
        select(Forwarding.id, Forwarding.name).where(Forwarding.user_id == user.id)
    )
    fwd_rows = fwd_result.all()
    fwd_ids = [row[0] for row in fwd_rows]
    fwd_names = {row[0]: row[1] for row in fwd_rows}

    if not fwd_ids:
        return {"logs": []}

    # Loglarni olish
    logs_result = await db.execute(
        select(ForwardLog)
        .where(ForwardLog.forwarding_id.in_(fwd_ids))
        .order_by(desc(ForwardLog.forwarded_at))
        .limit(50)
    )
    logs = logs_result.scalars().all()

    return {
        "logs": [
            {
                "id": log.id,
                "forwarding_id": log.forwarding_id,
                "forwarding_name": fwd_names.get(log.forwarding_id, "?"),
                "source_message_id": log.source_message_id,
                "dest_message_id": log.dest_message_id,
                "status": log.status,
                "error_message": log.error_message,
                "forwarded_at": log.forwarded_at.isoformat(),
            }
            for log in logs
        ]
    }


# ── API: Dashboard statistikasi ─────────────
@app.get("/api/stats/{telegram_id}")
async def get_stats(telegram_id: int, db: AsyncSession = Depends(get_db)):
    """Dashboard uchun statistika qaytaradi."""
    from datetime import datetime, date
    from sqlalchemy import func

    user_result = await db.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = user_result.scalar_one_or_none()

    if not user:
        return {"total_forwardings": 0, "active_forwardings": 0, "total_accounts": 0, "today_count": 0}

    # Jami forwardinglar
    total_result = await db.execute(
        select(func.count(Forwarding.id)).where(Forwarding.user_id == user.id)
    )
    total_forwardings = total_result.scalar_one() or 0

    # Aktiv forwardinglar
    active_result = await db.execute(
        select(func.count(Forwarding.id)).where(
            Forwarding.user_id == user.id,
            Forwarding.is_active == True,
        )
    )
    active_forwardings = active_result.scalar_one() or 0

    # Ulangan akkauntlar
    accounts_result = await db.execute(
        select(func.count(UserAccount.id)).where(
            UserAccount.user_id == user.id,
            UserAccount.is_active == True,
        )
    )
    total_accounts = accounts_result.scalar_one() or 0

    # Bugungi forward soni
    today_start = datetime.combine(date.today(), datetime.min.time())
    fwd_ids_result = await db.execute(
        select(Forwarding.id).where(Forwarding.user_id == user.id)
    )
    fwd_ids = [row[0] for row in fwd_ids_result.all()]

    today_count = 0
    if fwd_ids:
        today_result = await db.execute(
            select(func.count(ForwardLog.id)).where(
                ForwardLog.forwarding_id.in_(fwd_ids),
                ForwardLog.forwarded_at >= today_start,
                ForwardLog.status == "success",
            )
        )
        today_count = today_result.scalar_one() or 0

    return {
        "total_forwardings": total_forwardings,
        "active_forwardings": active_forwardings,
        "total_accounts": total_accounts,
        "today_count": today_count,
    }


# ── API: initData tekshirish ─────────────────
@app.post("/api/validate")
async def validate_init_data(body: ValidateInitDataRequest):
    """Mini App initData ni tekshiradi va foydalanuvchi ma'lumotlarini qaytaradi."""
    user_data = verify_telegram_init_data(body.init_data)
    if not user_data:
        raise HTTPException(status_code=401, detail="Noto'g'ri initData")
    return {"valid": True, "user": user_data}
