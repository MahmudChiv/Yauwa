"""Coordinate one trader voice note with ledger and reply services."""

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from app.core.config import Settings, get_settings
from app.db.ledger import record_sales
from app.db.session import get_engine
from app.models.trader import Trader
from app.schemas.extraction import StockItem
from app.services.extraction import (
    _stock_size_question,
    extract_data_from_audio,
    extract_package_sizes_from_audio,
)
from app.services.inventory import save_stock_items
from app.services.market import process_restock_confirmation
from app.services.market_delivery import send_market_list
from app.services.onboarding import get_trader_by_phone, send_onboarding_reply

logger = logging.getLogger(__name__)

PENDING_STOCK_TTL_SECONDS = 30 * 60
EXTRACTION_RETRY_REPLY = (
    "I get problem processing your voice note right now. Abeg try send am again later."
)


@dataclass
class PendingStock:
    items: list[StockItem]
    created_at: float


_pending_stock: dict[str, PendingStock] = {}


def _normalize_bulk_type(value: str | None) -> str | None:
    if value is None:
        return None
    return {
        "cartons": "carton",
        "bags": "bag",
        "packs": "pack",
        "rolls": "roll",
    }.get(value.casefold(), value)


def _get_pending_stock(phone_number: str) -> PendingStock | None:
    pending = _pending_stock.get(phone_number)
    if pending and time.monotonic() - pending.created_at >= PENDING_STOCK_TTL_SECONDS:
        _pending_stock.pop(phone_number, None)
        return None
    return pending


def _print_stock_rows(phone_number: str, items: list[StockItem]) -> dict:
    rows = {
        "phone_number": phone_number,
        "items": [
            {
                "item_name": item.item_name,
                "unit_quantity": item.unit_quantity,
                "bulk_type": item.bulk_type,
                "bulk_quantity": item.bulk_quantity,
                "unit_price": None,
                "low_stock_threshold": None,
            }
            for item in items
        ],
    }
    print(f"Item rows for database (now saved via save_stock_items): {rows}")
    return rows

def _save_extracted_sales(phone_number: str, sales: list[dict]) -> dict:
    from sqlmodel import select

    from app.providers.twilio import normalize_phone_number

    with Session(get_engine()) as lookup_session:
        trader = lookup_session.exec(
            select(Trader).where(
                Trader.phone_number == normalize_phone_number(phone_number)
            )
        ).one_or_none()

        if trader is None or trader.id is None:
            raise ValueError("Trader was not found.")

        trader_id = trader.id

    with Session(get_engine()) as write_session:
        return record_sales(write_session, trader_id, sales)

def _get_trader_item_names(trader_id: int | None) -> list[str]:
    """Retrieve existing item names in trader inventory to assist AI matching."""
    if trader_id is None:
        return []
    try:
        from app.models.item import Item
        with Session(get_engine()) as session:
            items = session.exec(
                select(Item.item_name).where(Item.trader_id == trader_id)
            ).all()
            return [name for name in items if name]
    except Exception:
        logger.exception("Could not retrieve trader item names for trader %s", trader_id)
        return []


def _resolve_trader_id(phone_number: str) -> int | None:
    """Look up the trader ID for this conversation's phone number."""
    try:
        with Session(get_engine()) as session:
            trader = get_trader_by_phone(session, phone_number)
            return trader.id if trader else None
    except Exception:
        logger.exception("Could not look up trader ID for phone %s", phone_number)
        return None


def _get_trader_language(trader_id: int | None) -> str | None:
    """Return the stored language preference used to interpret ASR ambiguity."""
    if trader_id is None:
        return None
    try:
        with Session(get_engine()) as session:
            trader = session.get(Trader, trader_id)
            return trader.language if trader else None
    except Exception:
        logger.exception("Could not retrieve language for trader %s", trader_id)
        return None


async def process_trader_audio(
    phone_number: str,
    audio_path: str | Path,
    content_type: str,
    settings: Settings | None = None,
    trader_id: int | None = None,
) -> dict | None:
    """Extract and print a trader message, speaking any required follow-up."""
    effective_settings = settings or get_settings()
    resolved_trader_id = (
        trader_id if trader_id is not None else _resolve_trader_id(phone_number)
    )
    known_items = _get_trader_item_names(resolved_trader_id)
    preferred_language = _get_trader_language(resolved_trader_id)
    pending = _get_pending_stock(phone_number)
    if pending is not None:
        try:
            answer = await extract_package_sizes_from_audio(
                audio_path,
                content_type,
                pending.items,
                settings=effective_settings,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Trader package-size extraction failed")
            await send_onboarding_reply(
                phone_number, EXTRACTION_RETRY_REPLY, effective_settings
            )
            return None

        print(f"Groq package-size answer: {answer.model_dump()}")
        if answer.status == "answer":
            for size in answer.sizes:
                item = pending.items[size.item_index]
                item.unit_quantity = item.bulk_quantity * size.units_per_bulk
                if size.corrected_item_name:
                    item.item_name = size.corrected_item_name
            missing = [item for item in pending.items if item.unit_quantity is None]
            if missing:
                pending.created_at = time.monotonic()
                await send_onboarding_reply(
                    phone_number,
                    _stock_size_question(missing),
                    effective_settings,
                )
                return None

            _pending_stock.pop(phone_number, None)
            rows = _print_stock_rows(phone_number, pending.items)
            items_to_save = rows.get("items", [])
            saved = False
            if items_to_save and resolved_trader_id is not None:
                try:
                    save_stock_items(resolved_trader_id, items_to_save)
                    saved = True
                except Exception:
                    logger.exception(
                        "Failed to save bulk stock items for trader %s",
                        resolved_trader_id,
                    )

            if saved:
                reply = (
                    "Yauwa, I don calculate the pieces for your goods and I don save am."
                )
            else:
                reply = (
                    "Yauwa, I don calculate the pieces for your goods. "
                    "I never save am yet; I dey test the stock list."
                )
            await send_onboarding_reply(
                phone_number,
                reply,
                effective_settings,
            )
            return rows
        if answer.status == "unclear":
            await send_onboarding_reply(
                phone_number,
                _stock_size_question(pending.items),
                effective_settings,
            )
            return None
    try:
        result = await extract_data_from_audio(
            audio_path,
            content_type,
            settings=effective_settings,
            known_items=known_items,
            preferred_language=preferred_language,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Trader stock and sales extraction failed")
        await send_onboarding_reply(
            phone_number,
            EXTRACTION_RETRY_REPLY,
            effective_settings,
        )
        return None

    print(f"Groq stock and sales extraction: {result}")
    if pending is not None and answer.status == "new_message":
        _pending_stock.pop(phone_number, None)
    if result["intent"] == "market_list":
        result["reply_text"] = None
        result["market_result"] = await send_market_list(
            phone_number, resolved_trader_id, effective_settings
        )
        return result
    if result["intent"] == "restock_confirmation":
        if resolved_trader_id is not None and result["status"] == "ready":
            items_payload = [
                item.model_dump() if hasattr(item, "model_dump") else item
                for item in result["stock_items"]
            ]
            try:
                process_restock_confirmation(
                    resolved_trader_id,
                    result.get("confirms_market_list", False),
                    items_payload,
                )
                stock_saved = True
                result["reply_text"] = (
                    "Yauwa, I don update your stock with the market restock wey you buy."
                )
            except Exception:
                logger.exception(
                    "Failed to process restock confirmation for trader %s",
                    resolved_trader_id,
                )
                result["reply_text"] = (
                    "I no fit update your restock right now. Abeg try send am again."
                )
    stock_saved = False
    if result["intent"] == "stock_intake":
        items = [StockItem.model_validate(item) for item in result["stock_items"]]
        for item in items:
            item.bulk_type = _normalize_bulk_type(item.bulk_type)
        missing = [item for item in items if item.unit_quantity is None]
        if missing and all(
            item.bulk_type and item.bulk_quantity is not None for item in missing
        ):
            _pending_stock[phone_number] = PendingStock(items, time.monotonic())
            result["reply_text"] = _stock_size_question(missing)
        elif result["status"] == "ready":
            rows = _print_stock_rows(phone_number, items)
            items_to_save = rows.get("items", [])
            if items_to_save and resolved_trader_id is not None:
                try:
                    save_stock_items(resolved_trader_id, items_to_save)
                    stock_saved = True
                except Exception:
                    logger.exception(
                        "Failed to save stock items for trader %s",
                        resolved_trader_id,
                    )
    reply = result["reply_text"]
    if result["status"] == "ready":
        if result["intent"] == "sale":
            try:
                summary = await asyncio.to_thread(
                    _save_extracted_sales,
                    phone_number,
                    result["sales"],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Could not record the sale batch")
                reply = (
                    "I no fit confirm say these sales don save. "
                    "Abeg make we check the record before you send am again."
                )
            else:
                if summary["unmatched_items"]:
                    reply = (
                        "I don record your sales, but some items no match "
                        "your stock list, so I no reduce their stock."
                    )
                else:
                    reply = "I don record your sales and update your stock."

                for sale in summary.get("sales", []):
                    if sale.get("alert_needed"):
                        name = sale.get("trader_name") or "Oga"
                        remaining = sale.get("remaining_quantity") or 0
                        item_name = sale.get("item_name")

                        if remaining <= 0:
                            alert_text = (
                                f"{name}, your {item_name} don finish completely. "
                                "You no get any remain. Make you go market buy more."
                            )
                        else:
                            alert_text = (
                                f"{name}, your {item_name} don dey finish. "
                                f"Only {remaining} remain. "
                                "Make you go market buy more before customers finish am."
                            )

                        try:
                            await send_onboarding_reply(
                                phone_number, alert_text, effective_settings
                            )
                        except Exception:
                            logger.exception("Low-stock alert send failed")
        elif result["intent"] == "stock_intake":
            reply = (
                "Yauwa, I don hear your goods and I don save am."
                if stock_saved
                else "Yauwa, I don hear your goods. I never save am yet; I dey test am."
            )

    result["reply_text"] = reply

    if reply:
        await send_onboarding_reply(
            phone_number,
            reply,
            effective_settings,
        )

    return result
