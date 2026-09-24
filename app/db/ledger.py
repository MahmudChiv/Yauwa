"""Record validated sales and decrement a trader's inventory."""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlmodel import Session, select

from app.db.money import money, sale_total
from app.models.item import Item
from app.models.sale import Sale
from app.models.trader import Trader

logger = logging.getLogger(__name__)


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

        for sale, total in zip(validated, totals):
            # Case-insensitive exact match within this trader's inventory.
            # No fuzzy guessing between brands or similar products.
            matches = session.exec(
                select(Item)
                .where(
                    Item.trader_id == trader_id,
                    func.lower(func.trim(Item.item_name))
                    == sale.item_name.lower(),
                )
                .order_by(Item.id)
                .with_for_update()
            ).all()

            if len(matches) > 1:
                raise SaleWriteError(
                    f"More than one item matches {sale.item_name}."
                )

            item = matches[0] if matches else None
            remaining_quantity = None

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