"""Money and discount calculations."""

from decimal import Decimal, ROUND_HALF_UP


CENT = Decimal("0.01")


def money(value: str | int | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def line_total(unit_price: str | int | Decimal, quantity: int) -> Decimal:
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    return money(Decimal(str(unit_price)) * quantity)


def discount_total(subtotal: Decimal, percent: str | int | Decimal) -> Decimal:
    """Return the discount amount for a percentage in [0, 100]."""
    rate = Decimal(str(percent))
    if rate < 0 or rate > 100:
        raise ValueError("discount percent must be between 0 and 100")
    return money(subtotal * rate / 100)
