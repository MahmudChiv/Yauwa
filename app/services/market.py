import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlmodel import Session, delete, select

from app.db.session import get_engine
from app.models.item import Item
from app.models.low_stock_item import LowStockItem

_last_market_list: dict[int, list[dict]] = {}


def _normalize_key(name: str) -> str:
    """Normalize item name for matching."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


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


def save_sent_market_list(trader_id: int, items: list[MarketItem]) -> None:
    """Record sent market list as reference point for future confirmation."""
    _last_market_list[trader_id] = [
        {
            "item_id": m.item_id,
            "item_name": m.item_name,
            "remaining": m.remaining,
            "suggested_quantity": m.suggested_quantity,
        }
        for m in items
    ]


def get_last_sent_market_list(trader_id: int) -> list[dict]:
    """Retrieve last sent market list or derive from live market items."""
    if trader_id in _last_market_list:
        return list(_last_market_list[trader_id])
    items = get_market_items(trader_id)
    return [
        {
            "item_id": m.item_id,
            "item_name": m.item_name,
            "remaining": m.remaining,
            "suggested_quantity": m.suggested_quantity,
        }
        for m in items
    ]


def process_restock_confirmation(
    trader_id: int,
    confirms_market_list: bool,
    stock_items: list[dict],
) -> dict:
    """Merge restocked items into inventory, update prices, and clear low stock queue.

    Parameters:
        trader_id: Primary key of the trader.
        confirms_market_list: True if trader confirmed buying the sent market list.
        stock_items: List of StockItem extractions containing modifications or additions.

    Returns:
        Summary dict containing list of updated items and cleared low-stock count.
    """
    ref_list = get_last_sent_market_list(trader_id) if confirms_market_list else []
    restock_map: dict[str, dict] = {}

    # 1. Initialize map from reference market list if confirmed
    if confirms_market_list:
        for m in ref_list:
            key = _normalize_key(m["item_name"])
            suggested = m["suggested_quantity"] or 0
            restock_map[key] = {
                "item_name": m["item_name"],
                "quantity": suggested,
                "unit_price": None,
                "bulk_type": None,
                "bulk_quantity": None,
            }

    # 2. Apply modifications and additions from stock_items
    for item in stock_items:
        raw_name = item.get("item_name") or ""
        key = _normalize_key(raw_name)
        action = (item.get("action") or "").lower()
        qty = item.get("unit_quantity")
        price = item.get("unit_price")
        btype = item.get("bulk_type")
        bqty = item.get("bulk_quantity")

        if key in restock_map:
            # Modify existing reference item
            if action == "remove":
                restock_map.pop(key, None)
                continue
            elif action == "add":
                restock_map[key]["quantity"] += (qty or 0)
            elif action == "reduce":
                restock_map[key]["quantity"] = max(0, restock_map[key]["quantity"] - (qty or 0))
            elif action == "set" or qty is not None:
                if qty is not None:
                    restock_map[key]["quantity"] = qty
            if price is not None:
                restock_map[key]["unit_price"] = price
            if btype is not None:
                restock_map[key]["bulk_type"] = btype
            if bqty is not None:
                restock_map[key]["bulk_quantity"] = bqty
        else:
            # Add item not on reference list (or standard stock intake)
            restock_map[key] = {
                "item_name": raw_name,
                "quantity": qty or 0,
                "unit_price": price,
                "bulk_type": btype,
                "bulk_quantity": bqty,
            }

    # 3. Apply updates to database
    now = datetime.now(timezone.utc)
    updated_items = []
    cleared_count = 0

    with Session(get_engine()) as session:
        existing_items = session.exec(
            select(Item).where(Item.trader_id == trader_id)
        ).all()
        existing_map = {_normalize_key(i.item_name): i for i in existing_items}

        for key, info in restock_map.items():
            qty_add = info["quantity"]
            target_item = existing_map.get(key)

            if target_item is not None:
                # Merge into existing Item row
                current_qty = target_item.unit_quantity if target_item.unit_quantity is not None else 0
                target_item.unit_quantity = current_qty + qty_add
                if info["unit_price"] is not None:
                    target_item.unit_price = info["unit_price"]
                if info["bulk_type"] is not None:
                    target_item.bulk_type = info["bulk_type"]
                if info["bulk_quantity"] is not None:
                    target_item.bulk_quantity = info["bulk_quantity"]
                target_item.last_updated = now
                session.add(target_item)
                updated_items.append({"item_name": target_item.item_name, "quantity": target_item.unit_quantity})
            else:
                # Insert brand new Item row
                threshold = (qty_add * 0.10) if qty_add is not None else 0.0
                new_item = Item(
                    trader_id=trader_id,
                    item_name=info["item_name"],
                    unit_quantity=qty_add,
                    bulk_type=info["bulk_type"],
                    bulk_quantity=info["bulk_quantity"],
                    unit_price=info["unit_price"],
                    low_stock_threshold=threshold,
                    last_updated=now,
                )
                session.add(new_item)
                updated_items.append({"item_name": new_item.item_name, "quantity": new_item.unit_quantity})

        session.flush()

        # 4. Clear lowStockItem entries for items now above threshold
        for key in list(restock_map.keys()) + list(existing_map.keys()):
            item_row = existing_map.get(key)
            if item_row is not None and item_row.unit_quantity is not None:
                if item_row.unit_quantity > item_row.low_stock_threshold:
                    del_stmt = delete(LowStockItem).where(
                        LowStockItem.item_id == item_row.id,
                        LowStockItem.trader_id == trader_id,
                    )
                    res = session.exec(del_stmt)
                    cleared_count += (res.rowcount if hasattr(res, "rowcount") and res.rowcount else 0)

        session.commit()

    # Clear saved market list after restock confirmation processed
    _last_market_list.pop(trader_id, None)

    return {
        "status": "success",
        "restocked_items": updated_items,
        "cleared_low_stock_count": cleared_count,
    }

