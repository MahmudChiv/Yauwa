"""Validated stock, sale, and package-size extraction data."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StockItem(BaseModel):
    """One product from the trader's initial stock list or restock confirmation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    item_name: str = Field(min_length=1, max_length=160)
    unit_quantity: int | None = Field(default=None, ge=0)
    bulk_type: str | None = Field(default=None, min_length=1, max_length=40)
    bulk_quantity: int | None = Field(default=None, gt=0)
    unit_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    action: str | None = Field(default=None, min_length=1, max_length=40)


class ExtractedSale(BaseModel):
    """One completed whole-unit retail sale."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    item_name: str = Field(min_length=1, max_length=160)
    quantity: int | None = Field(gt=0)
    unit_price: float | None = Field(ge=0, allow_inf_nan=False)
    total_price: float | None = Field(ge=0, allow_inf_nan=False)
    buyer_name: str | None = Field(min_length=1, max_length=160)


class ExtractionResult(BaseModel):
    """Validated interpretation of one independent voice note."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    intent: Literal["stock_intake", "sale", "market_list", "restock_confirmation", "unknown"]
    status: Literal["ready", "needs_clarification", "off_topic"]
    transcript: str = Field(min_length=1, max_length=3000)
    stock_items: list[StockItem] = Field(max_length=50)
    sales: list[ExtractedSale] = Field(max_length=50)
    confirms_market_list: bool = Field(default=False)
    reply_text: str | None = Field(max_length=500)


class PackageSize(BaseModel):
    """An explicitly stated number of units in one pending bulk package."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    item_index: int = Field(ge=0)
    units_per_bulk: int = Field(gt=0)
    corrected_item_name: str | None = Field(default=None, min_length=1, max_length=160)


class PackageSizeAnswer(BaseModel):
    """Interpretation of the next voice note while stock confirmation is pending."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["answer", "new_message", "unclear"]
    transcript: str = Field(min_length=1, max_length=3000)
    sizes: list[PackageSize] = Field(max_length=50)
