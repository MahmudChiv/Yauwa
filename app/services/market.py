"""Read-only adapter for the existing low-stock queue. No inventory writes."""

from dataclasses import dataclass
from decimal import Decimal

from sqlmodel import Session, select

from app.db.session import get_engine
from app.models.item import Item
from app.models.low_stock_item import LowStockItem


@dataclass(frozen=True)
class MarketItem:
    item_id: int
    item_name: str
    remaining: int | None
    suggested_quantity: int | None


def suggested_purchase(threshold: float, remaining: int | None) -> int | None:
    """The team defines threshold as 10% of first stock, in individual units."""
    if remaining is None or remaining < 0:
        return None
    target = Decimal(str(threshold)) * 10
    if not target.is_finite() or target <= 0:
        return None
    # Tolerate binary float noise from initial_quantity * 0.10, not real fractions.
    whole = target.to_integral_value()
    if abs(target - whole) > Decimal('0.000001'):
        return None
    return max(0, int(whole) - remaining)


def get_market_items(trader_id: int) -> list[MarketItem]:
    """Join queue entries to live inventory, scoped on both trader IDs.

    Restock logic owns clearing queue entries. Ignore recovered rows here.
    Unknown quantities get a clarification, never an invented purchase count.
    """
    with Session(get_engine()) as session:
        rows = session.exec(
            select(Item)
            .join(LowStockItem, LowStockItem.item_id == Item.id)
            .where(Item.trader_id == trader_id, LowStockItem.trader_id == trader_id)
            .order_by(Item.item_name, Item.id)
        ).all()
        return [
            MarketItem(item.id, item.item_name, item.unit_quantity,
                       suggested_purchase(item.low_stock_threshold, item.unit_quantity))
            for item in rows
            if item.unit_quantity is None or item.unit_quantity <= item.low_stock_threshold
        ]
