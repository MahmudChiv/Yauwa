"""Inventory business logic and stock persistence services.

This module provides transaction-safe operations to save, update,
and manage product inventory rows for retail traders.
"""

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlmodel import insert

from app.db.session import get_session
from app.models.item import Item


def save_stock_items(trader_id: int, items: list[dict]) -> int:
    """Save a batch of extracted stock items to the database for one trader.

    Parameters:
        trader_id: The primary key of the Trader who owns this inventory.
        items: List of dictionaries matching the StockItem extraction output,
            each containing item_name, unit_quantity, bulk_type, and bulk_quantity.

    Returns:
        The total number of Item rows successfully inserted.

    Raises:
        Exception: If any item fails to insert, the entire transaction is rolled
            back and the exception is re-raised (all-or-nothing atomicity).
    """
    if not items:
        return 0

    # Capture the exact timestamp in UTC for this stock intake batch.
    now = datetime.now(timezone.utc)

    # Transform each extracted dictionary into an insert-ready Item record.
    rows: list[dict] = []
    for item in items:
        unit_qty = item.get("unit_quantity")

        # Threshold defaults to 10% of known units; if units are still unknown
        # (pending bulk size confirmation), default to 0.0 until confirmed.
        low_stock_threshold = (unit_qty * 0.10) if unit_qty is not None else 0.0

        rows.append(
            {
                "trader_id": trader_id,
                "item_name": item["item_name"],
                "unit_quantity": unit_qty,
                "bulk_type": item.get("bulk_type"),
                "bulk_quantity": item.get("bulk_quantity"),
                "unit_price": None,
                "low_stock_threshold": low_stock_threshold,
                "last_updated": now,
            }
        )

    # Ensure get_session() functions cleanly as a context manager both
    # in application code and when wrapped by test mocks.
    session_cm = get_session()
    if not hasattr(session_cm, "__enter__"):
        session_cm = contextmanager(get_session)()

    # Execute atomic bulk insert: if any item fails, roll back the whole batch.
    with session_cm as session:
        try:
            session.exec(insert(Item), params=rows)
            session.commit()
        except Exception:
            session.rollback()
            raise

    return len(rows)
