"""
forwarder.py — Xabarlarni filtrlash, o'zgartirish va forward qilish logikasi.
AutoPass Bot loyihasining asosiy xabar qayta ishlash moduli.
"""

import logging
from datetime import datetime, time
from typing import Optional

import pytz
from telethon import TelegramClient
from telethon.tl.types import Message, MessageMediaPhoto, MessageMediaDocument

from database import AsyncSessionLocal, ForwardLog, Forwarding

logger = logging.getLogger(__name__)

# O'zbekiston timezone
UZBEKISTAN_TZ = pytz.timezone("Asia/Tashkent")


# ─────────────────────────────────────────────
# FILTERING
# ─────────────────────────────────────────────
async def should_forward(
    message: Message,
    forwarding: Forwarding,
) -> bool:
    """
    Xabarni forward qilish kerakmi yoki yo'qligini aniqlaydi.
    Barcha filtrlash qoidalarini tekshiradi.
    
    Returns:
        True  — forward qilinsin
        False — o'tkazib yuborilsin
    """
    filters = forwarding.filters or {}

    # Xabar matni (caption ham bo'lishi mumkin)
    text = ""
    if message.text:
        text = message.text.lower()
    elif message.message:
        text = message.message.lower()

    # ── 1. Keyword Whitelist ──────────────────
    # Faqat shu so'zlardan biri bo'lgan xabarlar o'tadi
    keyword_whitelist = filters.get("keyword_whitelist", [])
    if keyword_whitelist:
        # Ro'yxat bo'sh emas — hech bo'lmasa bitta kalit so'z bo'lishi kerak
        found = any(kw.lower() in text for kw in keyword_whitelist if kw)
        if not found:
            logger.debug(f"Forwarding {forwarding.id}: whitelist filter — o'tkazilmadi")
            return False

    # ── 2. Keyword Blacklist ──────────────────
    # Shu so'zlardan biri bo'lsa o'tkazilmaydi
    keyword_blacklist = filters.get("keyword_blacklist", [])
    if keyword_blacklist:
        blocked = any(kw.lower() in text for kw in keyword_blacklist if kw)
        if blocked:
            logger.debug(f"Forwarding {forwarding.id}: blacklist filter — bloklandi")
            return False

    # ── 3. Media Only ─────────────────────────
    # Faqat media xabarlar (rasm, video, fayl) o'tadi
    if filters.get("media_only", False):
        if not message.media:
            logger.debug(f"Forwarding {forwarding.id}: media_only — matn o'tkazilmadi")
            return False

    # ── 4. Text Only ──────────────────────────
    # Faqat matnli xabarlar o'tadi
    if filters.get("text_only", False):
        if message.media:
            logger.debug(f"Forwarding {forwarding.id}: text_only — media o'tkazilmadi")
            return False

    # ── 5. Every Nth ──────────────────────────
    # Har N-chi xabarni o'tkazish
    every_nth = filters.get("every_nth", 1)
    if every_nth and every_nth > 1:
        # DB dan shu forwarding uchun oxirgi N ta logni sanash
        count = await _get_message_count_since_last_forwarded(forwarding.id)
        # every_nth=3 bo'lsa: 0,1 ni o'tkazib yuboramiz, 2 ni forward qilamiz
        if count % every_nth != (every_nth - 1):
            logger.debug(f"Forwarding {forwarding.id}: every_nth={every_nth}, count={count} — o'tkazilmadi")
            return False

    # ── 6. Schedule tekshirish ────────────────
    schedule = forwarding.schedule or {}
    if schedule.get("enabled", False):
        now_uz = datetime.now(UZBEKISTAN_TZ)
        start_hour = schedule.get("start_hour", 0)
        end_hour = schedule.get("end_hour", 23)

        current_hour = now_uz.hour
        if not (start_hour <= current_hour < end_hour):
            logger.debug(
                f"Forwarding {forwarding.id}: jadval filter — "
                f"hozir {current_hour}:00, ruxsat {start_hour}:00-{end_hour}:00"
            )
            return False

    return True


async def _get_message_count_since_last_forwarded(forwarding_id: int) -> int:
    """
    Shu forwarding uchun jami forward qilingan yoki o'tkazib yuborilgan
    xabarlar sonini qaytaradi. every_nth logikasi uchun ishlatiladi.
    """
    from sqlalchemy import select, func

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(func.count(ForwardLog.id)).where(
                    ForwardLog.forwarding_id == forwarding_id
                )
            )
            count = result.scalar_one_or_none() or 0
            return count
    except Exception as e:
        logger.error(f"every_nth count olishda xato: {e}")
        return 0


# ─────────────────────────────────────────────
# MODIFICATION
# ─────────────────────────────────────────────
def apply_modifications(text: str, modifications: dict) -> str:
    """
    Xabar matniga prefix, suffix va replacement qoidalarini qo'llaydi.
    
    Args:
        text: Asl xabar matni
        modifications: O'zgartirish sozlamalari dict
    
    Returns:
        O'zgartirilgan matn
    """
    if not modifications:
        return text

    result = text or ""

    # ── Replacements ──────────────────────────
    # Ketma-ket barcha almashtirish qoidalarini qo'llash
    replacements = modifications.get("replacements", [])
    for rule in replacements:
        from_text = rule.get("from", "")
        to_text = rule.get("to", "")
        if from_text:
            result = result.replace(from_text, to_text)

    # ── Prefix ────────────────────────────────
    prefix = modifications.get("prefix", "")
    if prefix:
        result = f"{prefix}{result}"

    # ── Suffix ────────────────────────────────
    suffix = modifications.get("suffix", "")
    if suffix:
        result = f"{result}{suffix}"

    return result


# ─────────────────────────────────────────────
# FORWARDING
# ─────────────────────────────────────────────
async def forward_message(
    client: TelegramClient,
    message: Message,
    forwarding: Forwarding,
    dest_chat_id: int,
) -> Optional[int]:
    """
    Xabarni manzilga yuboradi (copy yoki forward mode).
    
    Args:
        client: Telethon client
        message: Yuborilayotgan xabar
        forwarding: Forwarding qoidasi
        dest_chat_id: Manzil chat ID si
    
    Returns:
        Yuborilgan xabar ID si yoki None (xato bo'lsa)
    """
    modifications = forwarding.modifications or {}
    remove_caption = forwarding.remove_caption

    try:
        if forwarding.copy_mode:
            # ── Copy Mode ─────────────────────
            # Xabarni nusxa ko'chirish (muallif ko'rinmaydi)
            sent_message = await _copy_message(
                client=client,
                message=message,
                dest_chat_id=dest_chat_id,
                modifications=modifications,
                remove_caption=remove_caption,
            )
        else:
            # ── Forward Mode ──────────────────
            # Oddiy forward (muallif va original link ko'rinadi)
            sent_messages = await client.forward_messages(
                entity=dest_chat_id,
                messages=message.id,
                from_peer=message.peer_id,
            )
            # forward_messages ro'yxat qaytaradi
            if isinstance(sent_messages, list):
                sent_message = sent_messages[0] if sent_messages else None
            else:
                sent_message = sent_messages

        if sent_message:
            logger.info(
                f"✅ Forward qilindi: forwarding_id={forwarding.id}, "
                f"src_msg={message.id} → dest_msg={sent_message.id}"
            )
            return sent_message.id
        return None

    except Exception as e:
        logger.error(
            f"❌ Forward qilishda xato: forwarding_id={forwarding.id}, "
            f"msg_id={message.id}, error={e}"
        )
        raise


async def _copy_message(
    client: TelegramClient,
    message: Message,
    dest_chat_id: int,
    modifications: dict,
    remove_caption: bool,
) -> Optional[Message]:
    """
    Xabarni copy mode da yuboradi.
    Media, matn, caption barchasini to'g'ri handle qiladi.
    """

    # ── Faqat matnli xabar ────────────────────
    if not message.media:
        text = message.text or message.message or ""
        modified_text = apply_modifications(text, modifications)

        # Bo'sh matn bo'lsa yubormaymiz
        if not modified_text.strip():
            return None

        sent = await client.send_message(
            entity=dest_chat_id,
            message=modified_text,
            # Formatting (bold, italic va h.k.) saqlanadi
            formatting_entities=message.entities if not modifications else None,
        )
        return sent

    # ── Media xabar ───────────────────────────
    # Caption olish
    caption = ""
    if not remove_caption:
        caption = message.message or ""
        caption = apply_modifications(caption, modifications)

    # Rasm
    if isinstance(message.media, MessageMediaPhoto):
        sent = await client.send_file(
            entity=dest_chat_id,
            file=message.media.photo,
            caption=caption if not remove_caption else None,
        )
        return sent

    # Hujjat, video, audio, GIF va boshqa fayllar
    if isinstance(message.media, MessageMediaDocument):
        sent = await client.send_file(
            entity=dest_chat_id,
            file=message.media.document,
            caption=caption if not remove_caption else None,
        )
        return sent

    # Boshqa media turlari (location, contact, va h.k.)
    # Ular uchun oddiy forward ishlatamiz
    sent_messages = await client.forward_messages(
        entity=dest_chat_id,
        messages=message.id,
        from_peer=message.peer_id,
    )
    if isinstance(sent_messages, list):
        return sent_messages[0] if sent_messages else None
    return sent_messages


# ─────────────────────────────────────────────
# LOG YOZISH
# ─────────────────────────────────────────────
async def save_forward_log(
    forwarding_id: int,
    source_message_id: Optional[int],
    dest_message_id: Optional[int],
    status: str,
    error_message: Optional[str] = None,
) -> None:
    """
    Forward natijasini DB ga yozadi.
    
    Args:
        forwarding_id: Forwarding qoidasi ID si
        source_message_id: Manba xabar ID si
        dest_message_id: Manzil xabar ID si (forward bo'lsa)
        status: "success", "failed", "filtered"
        error_message: Xato xabari (agar bo'lsa)
    """
    try:
        async with AsyncSessionLocal() as session:
            log = ForwardLog(
                forwarding_id=forwarding_id,
                source_message_id=source_message_id,
                dest_message_id=dest_message_id,
                status=status,
                error_message=error_message,
                forwarded_at=datetime.utcnow(),
            )
            session.add(log)
            await session.commit()
    except Exception as e:
        logger.error(f"Log yozishda xato: {e}")


# ─────────────────────────────────────────────
# ASOSIY XABAR QAYTA ISHLASH PIPELINE
# ─────────────────────────────────────────────
async def process_new_message(
    client: TelegramClient,
    message: Message,
    forwardings: list[Forwarding],
) -> None:
    """
    Yangi xabar kelganda barcha mos forwardinglarni qayta ishlaydi.
    UserBot ning NewMessage handleridan chaqiriladi.
    
    Args:
        client: Telethon client (xabarni kim oldi)
        message: Kelgan xabar
        forwardings: Shu klientga tegishli barcha aktiv forwardinglar
    """
    # Xabar qaysi chatdan kelganini aniqlash
    source_chat_id = None
    try:
        source_chat_id = message.chat_id or (
            message.peer_id.channel_id if hasattr(message.peer_id, "channel_id") else None
        )
        if source_chat_id is None and hasattr(message.peer_id, "chat_id"):
            source_chat_id = message.peer_id.chat_id
    except Exception:
        pass

    if not source_chat_id:
        logger.warning("Xabar chat_id sini aniqlashda muammo")
        return

    # Shu source_chat_id ga mos forwardinglarni topish
    matching = [
        f for f in forwardings
        if f.is_active and (
            f.source_chat_id == source_chat_id
            or f.source_chat_id == -source_chat_id
            or f.source_chat_id == int(f"-100{abs(source_chat_id)}")
            if f.source_chat_id else False
        )
    ]

    if not matching:
        return

    for forwarding in matching:
        dest_chat_id = forwarding.dest_chat_id
        if not dest_chat_id:
            logger.warning(f"Forwarding {forwarding.id}: dest_chat_id yo'q")
            continue

        try:
            # ── Filtrlash ─────────────────────
            pass_filter = await should_forward(message, forwarding)

            if not pass_filter:
                # Filtered statusini log ga yozish
                await save_forward_log(
                    forwarding_id=forwarding.id,
                    source_message_id=message.id,
                    dest_message_id=None,
                    status="filtered",
                )
                continue

            # ── Forward qilish ────────────────
            dest_msg_id = await forward_message(
                client=client,
                message=message,
                forwarding=forwarding,
                dest_chat_id=dest_chat_id,
            )

            # ── Muvaffaqiyatli log ────────────
            await save_forward_log(
                forwarding_id=forwarding.id,
                source_message_id=message.id,
                dest_message_id=dest_msg_id,
                status="success",
            )

        except Exception as e:
            logger.error(
                f"❌ Forwarding {forwarding.id} xato: {e}"
            )
            # Xato logini yozish
            await save_forward_log(
                forwarding_id=forwarding.id,
                source_message_id=message.id,
                dest_message_id=None,
                status="failed",
                error_message=str(e),
            )
