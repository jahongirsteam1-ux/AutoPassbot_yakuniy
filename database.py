"""
database.py — SQLAlchemy modellari va ma'lumotlar bazasini ishga tushirish.
AutoPass Bot loyihasi uchun barcha DB modellari shu yerda.
Sync psycopg2 ishlatiladi (asyncpg Python 3.13 da ishlamaydi).
"""

import os
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey,
    Integer, JSON, String, Text, create_engine, select
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Database URL
# ─────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Railway ba'zan "postgres://" beradi, SQLAlchemy "postgresql://" talab qiladi
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# asyncpg ni psycopg2 ga almashtirish
if "postgresql+asyncpg://" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)

# ─────────────────────────────────────────────
# Engine va Session
# ─────────────────────────────────────────────
engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

Base = declarative_base()


# ─────────────────────────────────────────────
# MODEL: User
# ─────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(100), nullable=True)
    first_name = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    accounts = relationship("UserAccount", back_populates="user", cascade="all, delete-orphan")
    forwardings = relationship("Forwarding", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User id={self.id} telegram_id={self.telegram_id}>"


# ─────────────────────────────────────────────
# MODEL: UserAccount
# ─────────────────────────────────────────────
class UserAccount(Base):
    __tablename__ = "user_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    phone = Column(String(20), nullable=False)
    session_string = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="accounts")
    forwardings = relationship("Forwarding", back_populates="account", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<UserAccount id={self.id} phone={self.phone}>"


# ─────────────────────────────────────────────
# MODEL: Forwarding
# ─────────────────────────────────────────────
class Forwarding(Base):
    __tablename__ = "forwardings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("user_accounts.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(200), nullable=False)

    source_chat_id = Column(BigInteger, nullable=True)
    source_username = Column(String(100), nullable=True)
    dest_chat_id = Column(BigInteger, nullable=True)
    dest_username = Column(String(100), nullable=True)

    is_active = Column(Boolean, default=True, nullable=False)
    copy_mode = Column(Boolean, default=True, nullable=False)
    remove_caption = Column(Boolean, default=False, nullable=False)

    filters = Column(JSON, default=dict, nullable=False)
    modifications = Column(JSON, default=dict, nullable=False)
    schedule = Column(JSON, default=dict, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="forwardings")
    account = relationship("UserAccount", back_populates="forwardings")
    logs = relationship("ForwardLog", back_populates="forwarding", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Forwarding id={self.id} name={self.name}>"


# ─────────────────────────────────────────────
# MODEL: ForwardLog
# ─────────────────────────────────────────────
class ForwardLog(Base):
    __tablename__ = "forward_logs"

    id = Column(Integer, primary_key=True, index=True)
    forwarding_id = Column(Integer, ForeignKey("forwardings.id", ondelete="CASCADE"), nullable=False, index=True)
    source_message_id = Column(BigInteger, nullable=True)
    dest_message_id = Column(BigInteger, nullable=True)
    forwarded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(String(20), nullable=False, default="success")
    error_message = Column(Text, nullable=True)

    forwarding = relationship("Forwarding", back_populates="logs")

    def __repr__(self):
        return f"<ForwardLog id={self.id} status={self.status}>"


# ─────────────────────────────────────────────
# DB ni ishga tushirish
# ─────────────────────────────────────────────
async def init_db():
    """Barcha jadvallarni yaratadi. Server ishga tushganda chaqiriladi."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("✅ Ma'lumotlar bazasi muvaffaqiyatli ishga tushirildi.")
    except Exception as e:
        logger.error(f"❌ DB ishga tushirishda xato: {e}")
        raise


# ─────────────────────────────────────────────
# DB session context manager (FastAPI dependency)
# ─────────────────────────────────────────────
def get_db():
    """FastAPI dependency injection uchun DB session qaytaradi."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ─────────────────────────────────────────────
# Yordamchi funksiyalar
# ─────────────────────────────────────────────
def get_or_create_user_sync(
    session: Session,
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
) -> User:
    """Foydalanuvchini DB dan topadi yoki yangisini yaratadi (sync)."""
    user = session.query(User).filter(User.telegram_id == telegram_id).first()

    if not user:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
        )
        session.add(user)
        session.flush()
        logger.info(f"✅ Yangi foydalanuvchi yaratildi: {telegram_id}")
    else:
        if username and user.username != username:
            user.username = username
        if first_name and user.first_name != first_name:
            user.first_name = first_name

    return user


async def get_or_create_user(
    session,
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
) -> User:
    """Async wrapper — ichida sync ishlatadi (compatibility uchun)."""
    return get_or_create_user_sync(session, telegram_id, username, first_name)


def get_active_accounts_with_forwardings_sync(session: Session) -> list:
    """Barcha aktiv akkauntlar va forwardinglarni qaytaradi (sync)."""
    accounts = (
        session.query(UserAccount)
        .filter(UserAccount.is_active == True)
        .all()
    )
    # forwardinglarni yuklash
    for acc in accounts:
        _ = acc.forwardings
        _ = acc.user
    return accounts


async def get_active_accounts_with_forwardings(session) -> list:
    """Async wrapper."""
    return get_active_accounts_with_forwardings_sync(session)
