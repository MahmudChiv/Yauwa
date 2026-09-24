"""SQLModel class for the Sale entity.

A Sale records a single completed transaction. The bot creates a new row each
time a trader reports selling something via voice. Keeping one row per sale
(rather than updating stock directly) gives a full audit trail.
"""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class Sale(SQLModel, table=True):
    """One row per sale transaction recorded by a trader."""

    # Primary key — auto-incremented by the database.
    # None here tells SQLModel to let the database assign the value on INSERT.
    id: int | None = Field(default=None, primary_key=True)

    # Which trader made this sale. References Trader.id.
    # Indexed so we can quickly pull the full sales history for one trader.
    trader_id: int = Field(foreign_key="trader.id", index=True)

    # The name of the product sold, extracted from the trader's voice message.
    item_name: str

    # Number of individual retail units sold in this transaction.
    quantity: int

    # Selling price for one unit at the time of sale.
    unit_price: float

    # Pre-computed total: quantity multiplied by unit price.
    total_price: float

    # Optional name of the person who bought the item.
    # None if the trader didn't mention a buyer (very common for cash sales).
    buyer_name: str | None = Field(default=None, nullable=True)

    # When the sale happened. Defaults to "now" in UTC.
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
