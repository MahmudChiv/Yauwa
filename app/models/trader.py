"""SQLModel class for the Trader entity.

A Trader is a Nigerian informal trader who interacts with the bot via WhatsApp.
This table stores their identity and language preference
"""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class Trader(SQLModel, table=True):
    """One row per registered WhatsApp trader."""

    # Primary key — auto-incremented by the database.
    # None here tells SQLModel to let the database assign the value on INSERT.
    id: int | None = Field(default=None, primary_key=True)

    # The trader's WhatsApp number in E.164 format (e.g. +2348012345678).
    # Must be unique because we look up traders by phone number on every message.
    # Indexed so the lookup is fast even with many traders.
    phone_number: str = Field(unique=True, index=True)

    # The trader's display name (used when the bot greets them).
    name: str

    # Preferred reply language code, e.g. "pidgin".
    # The AI layer reads this to pick the right reply style.
    language: str

    # When the trader first registered. Defaults to "now" in UTC so we never
    # need to pass a value explicitly; the database or Python fills it in.
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
