"""Database-backed voice onboarding for WhatsApp traders."""

import asyncio
import logging
import time

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.providers.generation import (
    NameStatus,
    looks_like_bot_name,
    extract_stated_name,
)
from app.providers.tts import register_media, synthesize_speech
from app.providers.twilio import normalize_phone_number, _send_twilio_message, _validate_public_media_origin
from app.core.config import Settings
from app.db.session import get_engine
from app.models.trader import Trader

logger = logging.getLogger(__name__)

FALLBACK_REPLIES = {
    "ask_name": "Welcome! Wetin be your name? Abeg send am as clear voice note.",
    "confirm_name": "Yauwa {name}, tell me the market wey you get for shop make I dey help you track your sales.",
    "retry_name": "Abeg send another clear voice note tell me your name make I fit help you.",
    "unrelated": "I dey here to help track your shop goods and sales. Abeg send clear voice note tell me your name.",
    "voice_required": "Abeg send your name as voice note make we fit start.",
}


def trader_exists(session: Session, phone_number: str, name: str) -> bool:
    """Return true only for an exact phone-and-name match after trimming input."""
    phone = normalize_phone_number(phone_number)
    clean_name = name.strip()
    if not phone or not clean_name:
        return False
    statement = select(Trader.id).where(
        Trader.phone_number == phone,
        Trader.name == clean_name,
    )
    return session.exec(statement).first() is not None


def get_trader_by_phone(session: Session, phone_number: str) -> Trader | None:
    """Fetch the unique trader row for a normalized phone number."""
    phone = normalize_phone_number(phone_number)
    if not phone:
        return None
    return session.exec(select(Trader).where(Trader.phone_number == phone)).first()


def onboarding_complete(session: Session, phone_number: str) -> bool:
    """Return whether a phone belongs to a trader with a nonblank stored name."""
    trader = get_trader_by_phone(session, phone_number)
    return bool(
        trader
        and trader.name
        and trader.name.strip()
        and not looks_like_bot_name(trader.name)
    )


def ensure_pending_trader(session: Session, phone_number: str) -> Trader:
    """Create an incomplete trader, tolerating a concurrent first message."""
    phone = normalize_phone_number(phone_number)
    if not phone:
        raise ValueError("phone_number cannot be blank")
    trader = get_trader_by_phone(session, phone)
    if trader is not None:
        return trader
    trader = Trader(phone_number=phone, name=None, language="pidgin")
    session.add(trader)
    try:
        session.commit()
        session.refresh(trader)
        return trader
    except IntegrityError:
        session.rollback()
        existing = get_trader_by_phone(session, phone)
        if existing is None:
            raise
        return existing
    except Exception:
        session.rollback()
        raise


def _save_name(phone_number: str, name: str) -> str:
    """Persist a validated name in a fresh, short database transaction."""
    clean_name = " ".join(name.split()).strip(" .,!?:;-_")
    if not clean_name:
        raise ValueError("name cannot be blank")
    with Session(get_engine()) as session:
        trader = get_trader_by_phone(session, phone_number)
        if trader is None:
            trader = ensure_pending_trader(session, phone_number)
        if trader.name and trader.name.strip():
            return trader.name.strip()
        trader.name = clean_name
        try:
            session.add(trader)
            session.commit()
            session.refresh(trader)
            return trader.name or clean_name
        except Exception:
            session.rollback()
            raise


async def send_onboarding_reply(
    phone_number: str,
    text: str,
    settings: Settings,
) -> None:
    """Prefer an audio reply and use Pidgin text as the final fallback."""
    started_at = time.perf_counter()
    try:
        _validate_public_media_origin(settings)
        path = await synthesize_speech(text, settings)
        synthesized_at = time.perf_counter()
        token = await register_media(path)
        registered_at = time.perf_counter()
        base_url = str(settings.public_base_url).rstrip("/")
        media_url = f"{base_url}/api/v1/media/{token}"
        sid = await asyncio.to_thread(
            _send_twilio_message,
            settings,
            phone_number,
            media_url=media_url,
        )
        sent_at = time.perf_counter()
        logger.info(
            "Queued onboarding audio reply %s; tts=%.2fs media_register=%.2fs "
            "twilio=%.2fs total=%.2fs",
            sid,
            synthesized_at - started_at,
            registered_at - synthesized_at,
            sent_at - registered_at,
            sent_at - started_at,
        )
        return
    except Exception:
        logger.warning(
            "Audio reply failed after %.2fs; trying text",
            time.perf_counter() - started_at,
            exc_info=True,
        )

    try:
        sid = await asyncio.to_thread(
            _send_twilio_message,
            settings,
            phone_number,
            body=text,
        )
        logger.info(
            "Queued onboarding text fallback %s; total=%.2fs",
            sid,
            time.perf_counter() - started_at,
        )
    except Exception:
        logger.exception("Both audio and text onboarding delivery failed")


async def onboard_trader(
    phone_number: str,
    audio_path: str | None,
    content_type: str | None,
    settings: Settings,
) -> str:
    """Advance one incomplete trader through the voice-name onboarding loop."""
    with Session(get_engine()) as session:
        trader = ensure_pending_trader(session, phone_number)
        if trader.name and trader.name.strip():
            if not looks_like_bot_name(trader.name):
                return "user exist already"
            trader.name = None
            try:
                session.add(trader)
                session.commit()
            except Exception:
                session.rollback()
                raise

    purpose = "voice_required"
    extracted_name: str | None = None
    if audio_path and content_type:
        try:
            result = await extract_stated_name(audio_path, content_type, settings)
        except Exception:
            logger.warning("Name transcription or Groq extraction failed", exc_info=True)
            purpose = "retry_name"
        else:
            if result.status is NameStatus.FOUND and result.name:
                extracted_name = _save_name(phone_number, result.name)
                purpose = "confirm_name"
            elif result.status in {NameStatus.UNRELATED, NameStatus.UNSAFE}:
                purpose = "unrelated"
            else:
                purpose = "ask_name"

    reply = FALLBACK_REPLIES[purpose].format(name=extracted_name or "")
    await send_onboarding_reply(phone_number, reply, settings)
    return "onboarding complete" if extracted_name else "onboarding pending"
