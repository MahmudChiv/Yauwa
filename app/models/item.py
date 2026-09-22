"""SQLModel class for the Item entity.

An Item represents a product that a specific trader stocks and sells.
Each row tracks the current quantity on hand and the unit price so the bot
can report stock levels and warn the trader when stock runs low.
"""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class Item(SQLModel, table=True):
    """One row per product line per trader."""

    # Primary key — auto-incremented by the database.
    # None here tells SQLModel to let the database assign the value on INSERT.
    id: int | None = Field(default=None, primary_key=True)

    # Which trader owns this item. References Trader.id.
    # Indexed so we can quickly fetch all items for one trader.
    trader_id: int = Field(foreign_key="trader.id", index=True)

    # The name the trader uses for this product (e.g. "Indomie", "groundnut oil").
    # The AI extracts this from voice input, so spelling may vary — normalise upstream.
    item_name: str

    # How many units the trader currently has in stock.
    # Float to support fractional units (e.g. kg, litres).
    quantity: float

    # The price the trader charges per unit, in Naira.
    unit_price: float

    # When stock falls to or below this number the bot sends a restock alert.
    low_stock_threshold: float

    # When this row was last changed. Defaults to "now" in UTC.
    # Callers should update this whenever quantity or price changes.
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
