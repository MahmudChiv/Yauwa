"""Shared naira arithmetic: round to kobo using decimal half-up rounding."""

from decimal import ROUND_HALF_UP, Decimal, localcontext


def money(value: float | Decimal) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Money must be finite and nonnegative.")
    with localcontext() as context:
        context.prec = max(28, len(amount.as_tuple().digits) + abs(amount.adjusted()) + 8)
        return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def sale_total(unit_price: float, quantity: int) -> Decimal:
    price = money(unit_price)
    with localcontext() as context:
        context.prec = max(28, len(price.as_tuple().digits) + len(str(quantity)) + 8)
        return money(price * quantity)
