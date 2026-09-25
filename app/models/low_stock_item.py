"""SQLModel class for the LowStockItem entity.

Tracks items that have fallen to or below their low-stock threshold.
This table provides a clean queue for the restocking flow and prevents
repeated low-stock alerts during ongoing sales.
"""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class LowStockItem(SQLModel, table=True):
    """One row per item currently at or below its low-stock threshold."""

    # Primary key — auto-incremented by the database.
    # None here tells SQLModel to let the database assign the value on INSERT.
    id: int | None = Field(default=None, primary_key=True)

    # Which inventory item is low. References Item.id.
    # Unique constraint ensures each item appears at most once in the low-stock queue.
    # Indexed so lookups by item are fast during sale processing.
    item_id: int = Field(foreign_key="item.id", unique=True, index=True)

    # Which trader owns the low-stock item. References Trader.id.
    # Indexed so we can quickly query all low items for a single trader.
    trader_id: int = Field(foreign_key="trader.id", index=True)

    # When this item crossed into low stock. Defaults to "now" in UTC.
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
