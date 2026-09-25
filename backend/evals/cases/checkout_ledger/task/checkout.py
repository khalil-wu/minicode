"""Checkout orchestration with all-or-nothing inventory effects."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from inventory import Inventory
from pricing import discount_total, line_total, money


@dataclass(frozen=True)
class Line:
    sku: str
    unit_price: str
    quantity: int


@dataclass(frozen=True)
class Receipt:
    subtotal: Decimal
    discount: Decimal
    total: Decimal


class Checkout:
    def __init__(self, inventory: Inventory, catalog: Mapping[str, str]):
        self.inventory = inventory
        self.catalog = dict(catalog)

    def purchase(self, lines: list[Line], discount_percent: str = "0") -> Receipt:
        if not lines:
            raise ValueError("at least one line is required")
        reserved: list[tuple[str, int]] = []
        try:
            subtotal = money(0)
            for line in lines:
                if line.sku not in self.catalog:
                    raise KeyError(line.sku)
                if line.unit_price != self.catalog[line.sku]:
                    raise ValueError(f"price changed for {line.sku}")
                self.inventory.reserve(line.sku, line.quantity)
                reserved.append((line.sku, line.quantity))
                subtotal += line_total(line.unit_price, line.quantity)
            discount = discount_total(subtotal, discount_percent)
            total = money(subtotal - discount)
            for sku, quantity in reserved:
                self.inventory.commit(sku, quantity)
            return Receipt(subtotal, discount, total)
        except Exception:
            for sku, quantity in reversed(reserved):
                self.inventory.release(sku, quantity)
            raise
