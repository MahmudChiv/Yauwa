"""Record validated sales and decrement a trader's inventory."""

import difflib
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.db.money import money, sale_total
from app.models.item import Item
from app.models.low_stock_item import LowStockItem
from app.models.sale import Sale
from app.models.trader import Trader

logger = logging.getLogger(__name__)

_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
    "eighty": "80", "ninety": "90", "hundred": "100",
}


def _normalize_item_key(name: str) -> str:
    """Normalize item name for flexible matching (lowercase, no spaces/punctuation, singularized)."""
    s = name.lower()
    s = re.sub(r'\bninety\s*one\b', '91', s)
    s = re.sub(r'\bninety\s*two\b', '92', s)
    s = re.sub(r'\bninety\s*three\b', '93', s)
    s = re.sub(r'\bninety\s*four\b', '94', s)
    s = re.sub(r'\bninety\s*five\b', '95', s)
    s = re.sub(r'\bninety\s*six\b', '96', s)
    s = re.sub(r'\bninety\s*seven\b', '97', s)
    s = re.sub(r'\bninety\s*eight\b', '98', s)
    s = re.sub(r'\bninety\s*nine\b', '99', s)
    for word, digit in _NUMBER_WORDS.items():
        s = re.sub(r'\b' + word + r'\b', digit, s)
    s = re.sub(r'[^a-z0-9]', '', s)
    if len(s) > 3 and s.endswith('s') and not s.endswith('ss'):
        s = s[:-1]
    return s


class SaleWriteError(ValueError):
    """The sale batch cannot safely be recorded."""


class SaleInput(BaseModel):
    """Complete sale data required before writing to PostgreSQL."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
    )

    item_name: str = Field(min_length=1, max_length=160)
    quantity: int = Field(gt=0)
    unit_price: float = Field(ge=0, allow_inf_nan=False)
    total_price: float = Field(ge=0, allow_inf_nan=False)
    buyer_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )


def record_sales(
    session: Session,
    trader_id: int,
    sales: list[dict],
) -> dict:
    """Save one batch using a fresh session with no active transaction.

    This function owns the transaction. An error rolls back the whole
    batch. Returned results describe a successfully committed batch.
    """
    if session.in_transaction():
        raise SaleWriteError("Use a fresh session for recording sales.")

    if not sales:
        raise SaleWriteError("The sale list is empty.")

    if len(sales) > 50:
        raise SaleWriteError("A batch cannot contain more than 50 sales.")

    validated = [SaleInput.model_validate(sale) for sale in sales]

    # Validate arithmetic independently of Gemini.
    totals = []
    for sale in validated:
        expected = sale_total(sale.unit_price, sale.quantity)
        supplied = money(sale.total_price)

        if supplied != expected:
            raise SaleWriteError(
                f"Total price does not match quantity × price "
                f"for {sale.item_name}."
            )

        totals.append(expected)

    recorded = []
    unmatched_items = []

    with session.begin():
        # Serializes batches for the same trader through this function.
        trader = session.exec(
            select(Trader)
            .where(Trader.id == trader_id)
            .with_for_update()
        ).one_or_none()

        if trader is None:
            raise SaleWriteError("Trader does not exist.")

        trader_name = trader.name

        trader_items = session.exec(
            select(Item)
            .where(Item.trader_id == trader_id)
            .order_by(Item.id)
            .with_for_update()
        ).all()

        for sale, total in zip(validated, totals):
            # 1. Exact case-insensitive match within trader's inventory
            matches = [
                item for item in trader_items
                if item.item_name.strip().lower() == sale.item_name.strip().lower()
            ]

            # 2. Flexible normalized match (spacing, singular/plural, number formats)
            if not matches:
                sale_norm = _normalize_item_key(sale.item_name)
                matches = [
                    item for item in trader_items
                    if _normalize_item_key(item.item_name) == sale_norm
                ]

            # 3. Fuzzy similarity match for minor transcription typos (e.g. "Danomik" -> "Dano milk")
            if not matches and sale_norm:
                scored = []
                for item in trader_items:
                    item_norm = _normalize_item_key(item.item_name)
                    ratio = difflib.SequenceMatcher(None, sale_norm, item_norm).ratio()
                    if ratio >= 0.75:
                        scored.append((ratio, item))
                if scored:
                    scored.sort(key=lambda x: x[0], reverse=True)
                    if len(scored) == 1 or (scored[0][0] - scored[1][0] >= 0.05):
                        matches = [scored[0][1]]

            if len(matches) > 1:
                raise SaleWriteError(
                    f"More than one item matches {sale.item_name}."
                )

            item = matches[0] if matches else None
            remaining_quantity = None
            alert_needed = False

            if item is not None:
                if item.unit_quantity is None:
                    raise SaleWriteError(
                        f"Stock quantity is unknown for {item.item_name}."
                    )

                if item.unit_quantity < sale.quantity:
                    raise SaleWriteError(
                        f"Not enough recorded stock for {item.item_name}."
                    )

                item.unit_quantity -= sale.quantity
                item.last_updated = datetime.now(timezone.utc)
                remaining_quantity = item.unit_quantity
                if item.unit_quantity is not None and item.unit_quantity <= item.low_stock_threshold:
                    already_low = session.exec(
                        select(LowStockItem).where(LowStockItem.item_id == item.id)
                    ).first()
                    if already_low is None:
                        session.add(LowStockItem(item_id=item.id, trader_id=trader_id))
                    alert_needed = True
                session.add(item)
            else:
                unmatched_items.append(sale.item_name)

            row = Sale(
                trader_id=trader_id,
                item_name=item.item_name if item else sale.item_name,
                quantity=sale.quantity,
                unit_price=float(money(sale.unit_price)),
                total_price=float(total),
                buyer_name=sale.buyer_name,
            )
            session.add(row)

            # Makes this record and updated stock visible to later
            # entries in the same batch without committing yet.
            session.flush()

            recorded.append(
                {
                    "sale_id": row.id,
                    "item_name": row.item_name,
                    "quantity": row.quantity,
                    "total_price": row.total_price,
                    "stock_adjusted": item is not None,
                    "remaining_quantity": remaining_quantity,
                    "alert_needed": alert_needed,
                    "trader_name": trader_name,
                }
            )

    # The transaction has committed successfully at this point.
    for item_name in unmatched_items:
        logger.warning(
            "Sale recorded without stock adjustment: trader_id=%s item=%r",
            trader_id,
            item_name,
        )

    return {
        "status": "recorded",
        "recorded_count": len(recorded),
        "total_sales_amount": float(sum(totals, Decimal("0"))),
        "sales": recorded,
        "unmatched_items": unmatched_items,
    }