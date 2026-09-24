"""SQLModel class for the Item entity.

An Item represents a product that a specific trader stocks and sells.
Each row tracks the current whole-unit quantity and any bulk packaging the
trader used while stating their opening stock.
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

    # Total individual sellable units. It remains unknown until the trader
    # confirms how many pieces are inside each bulk package.
    unit_quantity: int | None = Field(default=None, nullable=True)

    # Optional package description from opening stock, e.g. 3 packs or 10 bags.
    bulk_type: str | None = Field(default=None, nullable=True)
    bulk_quantity: int | None = Field(default=None, nullable=True)

    # Selling price can be collected later without blocking stock capture.
    unit_price: float | None = Field(default=None, nullable=True)

    # When unit quantity falls to or below this number, notify the trader.
    low_stock_threshold: float

    # When this row was last changed. Defaults to "now" in UTC.
    # Callers should update this whenever quantity or price changes.
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
