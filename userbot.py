"""
userbot.py — Telethon orqali foydalanuvchi akkauntlarini boshqarish.
Har bir foydalanuvchi uchun alohida TelegramClient yaratadi va RAM da saqlaydi.
"""

import asyncio
import io
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telethon import TelegramClient, events
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from database import AsyncSessionLocal, Forwarding, UserAccount
from forwarder import process_new_message

load_dotenv()

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Environment variables
# ─────────────────────────────────────────────
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
FERNET_KEY = os.getenv("FERNET_KEY", "").encode()

# Fernet instance — session stringlarni shifrlash/deshifrlash uchun
fernet = Fernet(FERNET_KEY) if FERNET_KEY else None


# ─────────────────────────────────────────────
# Session shifrlash yordamchi funksiyalari
# ─────────────────────────────────────────────
def encrypt_session(session_string: str) -> str:
    """Session stringni Fernet bilan shifrlaydi."""
    if not fernet:
        raise ValueError("FERNET_KEY sozlanmagan!")
    encrypted = fernet.encrypt(session_string.encode())
    return encrypted.decode()


def decrypt_session(encrypted_string: str) -> str:
    """Shifrlangan session stringni deshifrlaydi."""
    if not fernet:
        raise ValueError("FERNET_KEY sozlanmagan!")
    decrypted = fernet.decrypt(encrypted_string.encode())
    return decrypted.decode()


# ─────────────────────────────────────────────
# UserBotManager — Barcha Telethon clientlarni boshqaradi
# ─────────────────────────────────────────────
class UserBotManager:
    """
    Barcha foydalanuvchi Telethon clientlarini boshqaradigan singleton manager.
    
    clients dict: {account_id: TelegramClient}
    temp_clients dict: {phone: TelegramClient} — login paytidagi vaqtinchalik clientlar
    phone_code_hashes dict: {phone: phone_code_hash} — SMS kod tasdiqlash uchun
    """

    def __init__(self):
        # Aktiv clientlar: {account_id: TelegramClient}
        self.clients: dict[int, TelegramClient] = {}

        # Vaqtinchalik login clientlari: {phone: TelegramClient}
        self.temp_clients: dict[str, TelegramClient] = {}

        # SMS kod hashlari: {phone: phone_code_hash}
        self.phone_code_hashes: dict[str, str] = {}

        logger.info("UserBotManager ishga tushirildi.")

    # ─────────────────────────────────────────
    # Client qo'shish (akkaunt ulash)
    # ─────────────────────────────────────────
    async def add_client(
        self,
        account_id: int,
        user_id: int,
        session_string: str,
        forwardings: list,
    ) -> bool:
        """
        Yangi Telethon client yaratadi va NewMessage handler qo'shadi.
        
        Args:
            account_id: DB dagi UserAccount ID si
            user_id: Foydalanuvchi ID si (DB)
            session_string: Telethon session string (DESHIFRLANGAN)
            forwardings: Bu akkaunt uchun forwarding qoidalari
        
        Returns:
            True — muvaffaqiyatli, False — xato
        """
        try:
            # Eski client bo'lsa avval o'chirish
            if account_id in self.clients:
                await self.remove_client(account_id)

            # Yangi client yaratish
            client = TelegramClient(
                StringSession(session_string),
                API_ID,
                API_HASH,
            )
            await client.connect()

            # Autentifikatsiya tekshirish
            if not await client.is_user_authorized():
                logger.warning(f"Akkaunt {account_id} autorizatsiyasi yo'q — o'tkazildi")
                await client.disconnect()
                return False

            # Clientni RAM ga saqlash
            self.clients[account_id] = client

            # NewMessage event handler qo'shish
            @client.on(events.NewMessage())
            async def new_message_handler(event):
                await _handle_new_message(event, account_id, user_id)

            logger.info(f"✅ Akkaunt {account_id} muvaffaqiyatli ulandi.")
            return True

        except Exception as e:
            logger.error(f"❌ Akkaunt {account_id} ulanishda xato: {e}")
            return False

    async def remove_client(self, account_id: int) -> None:
        """
        Clientni to'xtatadi va RAM dan o'chiradi.
        """
        client = self.clients.pop(account_id, None)
        if client:
            try:
                await client.disconnect()
                logger.info(f"✅ Akkaunt {account_id} o'chirildi.")
            except Exception as e:
                logger.error(f"Akkaunt {account_id} o'chirishda xato: {e}")

    # ─────────────────────────────────────────
    # Login jarayoni
    # ─────────────────────────────────────────
    async def send_code(self, phone: str) -> tuple[str, TelegramClient]:
        """
        Telefon raqamga SMS kod yuboradi.
        Vaqtinchalik client yaratadi va saqlaydi.
        
        Args:
            phone: Telefon raqam (+998901234567 formatda)
        
        Returns:
            (phone_code_hash, temp_client) tuple
        """
        # Eski temp client bo'lsa tozalash
        if phone in self.temp_clients:
            try:
                await self.temp_clients[phone].disconnect()
            except Exception:
                pass

        # Yangi vaqtinchalik client
        temp_client = TelegramClient(
            StringSession(),  # Bo'sh session — yangi login
            API_ID,
            API_HASH,
        )
        await temp_client.connect()

        # SMS kod yuborish
        result = await temp_client.send_code_request(phone)
        phone_code_hash = result.phone_code_hash

        # Saqlash
        self.temp_clients[phone] = temp_client
        self.phone_code_hashes[phone] = phone_code_hash

        logger.info(f"📱 Kod yuborildi: {phone}")
        return phone_code_hash, temp_client

    async def sign_in(
        self,
        phone: str,
        code: str,
    ) -> tuple[str, bool]:
        """
        SMS kod bilan tizimga kiradi.
        
        Args:
            phone: Telefon raqam
            code: SMS kod
        
        Returns:
            (session_string, needs_2fa) tuple
            needs_2fa=True bo'lsa 2FA parol kerak
        
        Raises:
            PhoneCodeInvalidError: Noto'g'ri kod
            PhoneCodeExpiredError: Kod muddati o'tgan
        """
        temp_client = self.temp_clients.get(phone)
        phone_code_hash = self.phone_code_hashes.get(phone)

        if not temp_client or not phone_code_hash:
            raise ValueError(f"Telefon {phone} uchun aktiv sessiya topilmadi. /login qaytadan bosing.")

        try:
            await temp_client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=phone_code_hash,
            )
            # Muvaffaqiyatli kirdi — session string olish
            session_string = temp_client.session.save()

            # Temp clientni tozalash
            self.temp_clients.pop(phone, None)
            self.phone_code_hashes.pop(phone, None)

            logger.info(f"✅ Tizimga kirdi: {phone}")
            return session_string, False

        except SessionPasswordNeededError:
            # 2FA kerak — temp client saqlanib qoladi
            logger.info(f"🔐 2FA kerak: {phone}")
            return "", True

    async def sign_in_2fa(
        self,
        phone: str,
        password: str,
    ) -> str:
        """
        2FA parol bilan tizimga kiradi.
        
        Args:
            phone: Telefon raqam
            password: 2FA paroli
        
        Returns:
            Session string
        
        Raises:
            PasswordHashInvalidError: Noto'g'ri parol
        """
        temp_client = self.temp_clients.get(phone)

        if not temp_client:
            raise ValueError(f"Telefon {phone} uchun aktiv sessiya topilmadi.")

        # 2FA parol bilan kirish
        await temp_client.sign_in(password=password)
        session_string = temp_client.session.save()

        # Temp clientni tozalash
        self.temp_clients.pop(phone, None)
        self.phone_code_hashes.pop(phone, None)

        logger.info(f"✅ 2FA bilan tizimga kirdi: {phone}")
        return session_string

    # ─────────────────────────────────────────
    # Chat ma'lumotlarini olish
    # ─────────────────────────────────────────
    async def get_chat_info(
        self,
        account_id: int,
        chat_identifier: str,
    ) -> Optional[tuple[int, str]]:
        """
        Chat username yoki link bo'yicha chat ID va nomini qaytaradi.
        
        Args:
            account_id: Qaysi akkaunt orqali tekshirish
            chat_identifier: username, t.me link yoki chat ID
        
        Returns:
            (chat_id, chat_title) yoki None
        """
        client = self.clients.get(account_id)
        if not client:
            logger.error(f"Akkaunt {account_id} topilmadi")
            return None

        try:
            # t.me/username yoki @username formatlarini tozalash
            identifier = chat_identifier.strip()
            if identifier.startswith("https://t.me/"):
                identifier = identifier.replace("https://t.me/", "@")
            elif identifier.startswith("t.me/"):
                identifier = identifier.replace("t.me/", "@")
            elif not identifier.startswith("@") and not identifier.lstrip("-").isdigit():
                identifier = f"@{identifier}"

            entity = await client.get_entity(identifier)
            chat_id = entity.id
            chat_title = getattr(entity, "title", None) or getattr(entity, "username", str(chat_id))

            # Kanal ID larini to'g'ri formatga keltirish (manfiy)
            if hasattr(entity, "megagroup") or hasattr(entity, "broadcast"):
                if chat_id > 0:
                    chat_id = int(f"-100{chat_id}")

            logger.info(f"Chat topildi: {chat_title} ({chat_id})")
            return chat_id, chat_title

        except Exception as e:
            logger.error(f"Chat ma'lumoti olishda xato ({chat_identifier}): {e}")
            return None

    # ─────────────────────────────────────────
    # Barcha aktiv sessionlarni yuklash (restart uchun)
    # ─────────────────────────────────────────
    async def load_all_sessions(self) -> None:
        """
        DB dagi barcha aktiv akkauntlarni yuklab clientlarni yaratadi.
        Server restart bo'lganda chaqiriladi.
        """
        logger.info("🔄 DB dan aktiv sessionlar yuklanmoqda...")

        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(UserAccount)
                    .where(UserAccount.is_active == True)
                    .options(selectinload(UserAccount.forwardings))
                )
                accounts = result.scalars().all()

            loaded = 0
            failed = 0

            for account in accounts:
                try:
                    # Session stringni deshifrlash
                    plain_session = decrypt_session(account.session_string)

                    # Aktiv forwardinglarni filtrlash
                    active_forwardings = [
                        f for f in account.forwardings if f.is_active
                    ]

                    # Client qo'shish
                    success = await self.add_client(
                        account_id=account.id,
                        user_id=account.user_id,
                        session_string=plain_session,
                        forwardings=active_forwardings,
                    )

                    if success:
                        loaded += 1
                    else:
                        failed += 1

                except Exception as e:
                    logger.error(f"Akkaunt {account.id} yuklanmadi: {e}")
                    failed += 1

            logger.info(f"✅ Sessionlar yuklandi: {loaded} ta muvaffaqiyatli, {failed} ta xato")

        except Exception as e:
            logger.error(f"❌ Sessionlarni yuklashda xato: {e}")

    # ─────────────────────────────────────────
    # Forwarding yangilanganda handler ni yangilash
    # ─────────────────────────────────────────
    async def refresh_forwardings(self, account_id: int) -> None:
        """
        Forwarding qo'shilganda yoki o'zgartirilganda
        clientni qayta ishga tushiradi.
        """
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(UserAccount).where(UserAccount.id == account_id)
                )
                account = result.scalar_one_or_none()

                if not account or not account.is_active:
                    return

                # Forwardinglarni yangilash
                result2 = await session.execute(
                    select(Forwarding).where(
                        Forwarding.account_id == account_id,
                        Forwarding.is_active == True,
                    )
                )
                forwardings = result2.scalars().all()

            # Clientni qayta ishga tushirish
            if account_id in self.clients:
                plain_session = decrypt_session(account.session_string)
                await self.add_client(
                    account_id=account_id,
                    user_id=account.user_id,
                    session_string=plain_session,
                    forwardings=forwardings,
                )
                logger.info(f"Akkaunt {account_id} forwardingleri yangilandi.")

        except Exception as e:
            logger.error(f"Forwarding yangilashda xato: {e}")


# ─────────────────────────────────────────────
# NewMessage event handler (global)
# ─────────────────────────────────────────────
async def _handle_new_message(event, account_id: int, user_id: int) -> None:
    """
    Yangi xabar kelganda ishga tushadigan handler.
    DB dan shu akkauntga tegishli forwardinglarni olib process qiladi.
    """
    try:
        message = event.message

        # DB dan aktiv forwardinglarni olish
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Forwarding).where(
                    Forwarding.account_id == account_id,
                    Forwarding.is_active == True,
                )
            )
            forwardings = result.scalars().all()

        if not forwardings:
            return

        # Global manager dan clientni olish
        client = userbot_manager.clients.get(account_id)
        if not client:
            return

        # Xabarni qayta ishlash (filtrlash + forward)
        await process_new_message(
            client=client,
            message=message,
            forwardings=forwardings,
        )

    except Exception as e:
        logger.error(f"NewMessage handler xato (account {account_id}): {e}")


# ─────────────────────────────────────────────
# Global instance — butun dasturda bitta manager
# ─────────────────────────────────────────────
userbot_manager = UserBotManager()
