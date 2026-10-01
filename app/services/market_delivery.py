"""One short market reminder per product; reuses existing TTS and delivery."""

import asyncio
import logging
import math
from pathlib import Path

from mutagen.mp3 import MP3

from app.providers.twilio import _send_twilio_message, _validate_public_media_origin
from app.providers.tts import register_media, synthesize_speech
from app.core.config import Settings
from app.services.market import MarketItem, get_market_items, save_sent_market_list

logger = logging.getLogger(__name__)
MAX_AUDIO_SECONDS = 10.0
SEND_DELAY_SECONDS = 1.0


def item_replies(item: MarketItem) -> tuple[str, str]:
    """Keep product identities and counts factual; no generated prices or packs."""
    name = ' '.join(item.item_name.split())
    if item.remaining is None:
        return (f"{name}: abeg tell me how many pieces remain.",
                f"{name}: how many remain?")
    if item.suggested_quantity:
        return (f"{name} remain {item.remaining}. You fit buy {item.suggested_quantity} pieces.",
                f"{name}: you fit buy {item.suggested_quantity} pieces.")
    return (f"{name} remain {item.remaining}. Check how many you need buy.",
            f"{name}: remember to restock.")


def audio_duration(path: Path) -> float:
    return MP3(path).info.length


async def send_short_reply(phone: str, text: str, shorter: str, settings: Settings) -> str:
    """Return queued channel, or failed. Never submit audio longer than 10s.

    Try one shorter version when necessary. If TTS fails or remains too long,
    use the complete text so no brand/size is silently chopped off. A Twilio
    submission error is reported without retrying an uncertain accepted send.
    """
    path = None
    try:
        _validate_public_media_origin(settings)
        for wording in dict.fromkeys((text, shorter)):
            path = await synthesize_speech(wording, settings)
            duration = await asyncio.to_thread(audio_duration, path)
            if math.isfinite(duration) and 0 < duration <= MAX_AUDIO_SECONDS:
                break
            await asyncio.to_thread(path.unlink, missing_ok=True)
            path = None
        if path is None:
            raise ValueError('Market audio exceeded duration limit')
        token = await register_media(path)
        media_url = f"{str(settings.public_base_url).rstrip('/')}/api/v1/media/{token}"
    except asyncio.CancelledError:
        if path is not None:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        raise
    except Exception:
        logger.warning('Market audio unavailable; using text', exc_info=True)
        if path is not None:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        try:
            await asyncio.to_thread(_send_twilio_message, settings, phone, body=text)
            return 'text'
        except Exception:
            logger.exception('Market text submission failed')
            return 'failed'
    try:
        await asyncio.to_thread(_send_twilio_message, settings, phone, media_url=media_url)
        return 'audio'
    except Exception:
        logger.exception('Market audio submission failed; acceptance may be uncertain')
        return 'failed'


async def send_market_list(phone: str, trader_id: int | None, settings: Settings) -> dict:
    """Read recommendations and send in order without changing the database."""
    try:
        if trader_id is None:
            raise ValueError('Trader unavailable')
        items = await asyncio.to_thread(get_market_items, trader_id)
        save_sent_market_list(trader_id, items)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception('Could not load market list')
        text = 'I no fit check your market list now. Abeg try again later.'
        channel = await send_short_reply(phone, text, 'Abeg try your market list again later.', settings)
        return {'status': 'unavailable', 'deliveries': [{'channel': channel}]}

    if not items:
        text = 'No goods dey your low-stock list now.'
        channel = await send_short_reply(phone, text, text, settings)
        return {'status': 'empty', 'deliveries': [{'channel': channel}]}

    deliveries = []
    for index, item in enumerate(items):
        if index:
            await asyncio.sleep(SEND_DELAY_SECONDS)
        text, shorter = item_replies(item)
        channel = await send_short_reply(phone, text, shorter, settings)
        deliveries.append({'item_id': item.item_id, 'item_name': item.item_name,
                           'suggested_quantity': item.suggested_quantity,
                           'text': text, 'channel': channel})
    return {'status': 'partial' if any(d['channel'] == 'failed' for d in deliveries)
            else 'queued', 'deliveries': deliveries}
