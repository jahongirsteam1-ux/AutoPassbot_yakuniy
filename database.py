"""
database.py — SQLAlchemy modellari va ma'lumotlar bazasini ishga tushirish.
AutoPass Bot loyihasi uchun barcha DB modellari shu yerda.
"""

import os
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey,
    Integer, JSON, String, Text, create_engine
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Database URL (Railway PostgreSQL)
# ─────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")

# SQLAlchemy async engine uchun URL ni to'g'irlash
# Railway ba'zan "postgresql://" beradi, bizga "postgresql+asyncpg://" kerak
if DATABASE_URL.startswith("postgresql://"):
    ASYNC_DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    ASYNC_DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
else:
    ASYNC_DATABASE_URL = DATABASE_URL

# Sync engine (alembic va init uchun)
SYNC_DATABASE_URL = ASYNC_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)

# ─────────────────────────────────────────────
# Engine va Session yaratish
# ─────────────────────────────────────────────
async_engine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo=False,           # SQL loglarini ko'rsatish (debug uchun True qil)
    pool_pre_ping=True,   # Connection tirikligini tekshirish
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()


# ─────────────────────────────────────────────
# MODEL: User
# Botdan foydalanadigan Telegram foydalanuvchi
# ─────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(100), nullable=True)
    first_name = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Bog'liq akkauntlar va forwardinglar
    accounts = relationship("UserAccount", back_populates="user", cascade="all, delete-orphan")
    forwardings = relationship("Forwarding", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User id={self.id} telegram_id={self.telegram_id} username={self.username}>"


# ─────────────────────────────────────────────
# MODEL: UserAccount
# Foydalanuvchining ulangan Telegram akkaunti (Telethon session)
# ─────────────────────────────────────────────
class UserAccount(Base):
    __tablename__ = "user_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    phone = Column(String(20), nullable=False)
    # Session string Fernet bilan shifrlangan holda saqlanadi
    session_string = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Bog'liq modellar
    user = relationship("User", back_populates="accounts")
    forwardings = relationship("Forwarding", back_populates="account", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<UserAccount id={self.id} phone={self.phone} user_id={self.user_id}>"


# ─────────────────────────────────────────────
# MODEL: Forwarding
# Forwarding qoidasi: qaysi kanaldan qaysi kanalga
# ─────────────────────────────────────────────
class Forwarding(Base):
    __tablename__ = "forwardings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("user_accounts.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(200), nullable=False)

    # Manba kanal ma'lumotlari
    source_chat_id = Column(BigInteger, nullable=True)
    source_username = Column(String(100), nullable=True)

    # Manzil kanal ma'lumotlari
    dest_chat_id = Column(BigInteger, nullable=True)
    dest_username = Column(String(100), nullable=True)

    is_active = Column(Boolean, default=True, nullable=False)

    # True = copy (muallif ko'rinmaydi), False = forward (muallif ko'rinadi)
    copy_mode = Column(Boolean, default=True, nullable=False)

    # Media captionni olib tashlash
    remove_caption = Column(Boolean, default=False, nullable=False)

    # Filtrlash sozlamalari (JSON)
    # {
    #   "keyword_whitelist": ["kalit1", "kalit2"],
    #   "keyword_blacklist": ["spam", "reklama"],
    #   "media_only": false,
    #   "text_only": false,
    #   "every_nth": 1
    # }
    filters = Column(JSON, default=dict, nullable=False)

    # O'zgartirish sozlamalari (JSON)
    # {
    #   "prefix": "📢 ",
    #   "suffix": "\n\n@mychanel",
    #   "replacements": [{"from": "eski", "to": "yangi"}]
    # }
    modifications = Column(JSON, default=dict, nullable=False)

    # Jadval sozlamalari (JSON)
    # {
    #   "enabled": true,
    #   "start_hour": 8,
    #   "end_hour": 22,
    #   "timezone": "Asia/Tashkent"
    # }
    schedule = Column(JSON, default=dict, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Bog'liq modellar
    user = relationship("User", back_populates="forwardings")
    account = relationship("UserAccount", back_populates="forwardings")
    logs = relationship("ForwardLog", back_populates="forwarding", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Forwarding id={self.id} name={self.name} active={self.is_active}>"


# ─────────────────────────────────────────────
# MODEL: ForwardLog
# Har bir forward qilingan xabar tarixi
# ─────────────────────────────────────────────
class ForwardLog(Base):
    __tablename__ = "forward_logs"

    id = Column(Integer, primary_key=True, index=True)
    forwarding_id = Column(Integer, ForeignKey("forwardings.id", ondelete="CASCADE"), nullable=False, index=True)
    source_message_id = Column(BigInteger, nullable=True)
    dest_message_id = Column(BigInteger, nullable=True)
    forwarded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # "success", "failed", "filtered"
    status = Column(String(20), nullable=False, default="success")
    # Xato xabari (agar bo'lsa)
    error_message = Column(Text, nullable=True)

    # Bog'liq model
    forwarding = relationship("Forwarding", back_populates="logs")

    def __repr__(self):
        return f"<ForwardLog id={self.id} forwarding_id={self.forwarding_id} status={self.status}>"


# ─────────────────────────────────────────────
# DB ni ishga tushirish (barcha jadvallarni yaratish)
# ─────────────────────────────────────────────
async def init_db():
    """
    Barcha jadvallarni yaratadi (agar mavjud bo'lmasa).
    Server ishga tushganda chaqiriladi.
    """
    try:
        async with async_engine.begin() as conn:
            # Barcha jadvallarni yaratish
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✅ Ma'lumotlar bazasi muvaffaqiyatli ishga tushirildi.")
    except Exception as e:
        logger.error(f"❌ DB ishga tushirishda xato: {e}")
        raise


# ─────────────────────────────────────────────
# DB session context manager
# ─────────────────────────────────────────────
async def get_db() -> AsyncSession:
    """
    FastAPI dependency injection uchun DB session qaytaradi.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ─────────────────────────────────────────────
# Yordamchi funksiyalar
# ─────────────────────────────────────────────
async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
) -> User:
    """
    Foydalanuvchini DB dan topadi yoki yangisini yaratadi.
    """
    from sqlalchemy import select

    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = result.scalar_one_or_none()

    if not user:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
        )
        session.add(user)
        await session.flush()
        logger.info(f"✅ Yangi foydalanuvchi yaratildi: {telegram_id}")
    else:
        # Ma'lumotlarni yangilash (agar o'zgargan bo'lsa)
        if username and user.username != username:
            user.username = username
        if first_name and user.first_name != first_name:
            user.first_name = first_name

    return user


async def get_active_accounts_with_forwardings(session: AsyncSession) -> list:
    """
    Barcha aktiv akkauntlar va ularning forwardinglarini qaytaradi.
    Server restart bo'lganda Telethon clientlarini qayta yuklash uchun ishlatiladi.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    result = await session.execute(
        select(UserAccount)
        .where(UserAccount.is_active == True)
        .options(
            selectinload(UserAccount.forwardings),
            selectinload(UserAccount.user),
        )
    )
    return result.scalars().all()
