"""Inventory and reservation primitives for a small checkout service."""

from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True)
class StockItem:
    sku: str
    available: int


class Inventory:
    def __init__(self, items: dict[str, int]):
        self._items = {sku: StockItem(sku, int(quantity)) for sku, quantity in items.items()}
        self._reserved: dict[str, int] = {}
        self._lock = RLock()

    def available(self, sku: str) -> int:
        with self._lock:
            item = self._items.get(sku)
            if item is None:
                raise KeyError(sku)
            return item.available - self._reserved.get(sku, 0)

    def reserve(self, sku: str, quantity: int) -> None:
        """Reserve stock until commit or release."""
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        with self._lock:
            if self.available(sku) < quantity:
                raise ValueError(f"insufficient stock for {sku}")
            self._reserved[sku] = self._reserved.get(sku, 0) + quantity

    def release(self, sku: str, quantity: int) -> None:
        with self._lock:
            current = self._reserved.get(sku, 0)
            if quantity <= 0 or quantity > current:
                raise ValueError("invalid release")
            remaining = current - quantity
            if remaining:
                self._reserved[sku] = remaining
            else:
                self._reserved.pop(sku, None)

    def commit(self, sku: str, quantity: int) -> None:
        with self._lock:
            current = self._reserved.get(sku, 0)
            if quantity <= 0 or quantity > current:
                raise ValueError("invalid commit")
            item = self._items[sku]
            self._items[sku] = StockItem(sku, item.available - quantity)
            remaining = current - quantity
            if remaining:
                self._reserved[sku] = remaining
            else:
                self._reserved.pop(sku, None)
