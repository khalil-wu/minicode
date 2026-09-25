"""Regression tests for the checkout ledger contract."""

import threading
from decimal import Decimal

import pytest

from checkout import Checkout, Line
from inventory import Inventory
from pricing import discount_total, line_total, money


def service(stock=None):
    return Checkout(Inventory({"A": 5, "B": 3} if stock is None else stock), {"A": "10.005", "B": "2.50"})


def test_numerically_equal_price_format_is_accepted():
    """Catalog is authoritative; '10.0050' == '10.005' numerically."""
    receipt = service().purchase([Line("A", "10.0050", 1)])
    assert receipt.subtotal == Decimal("10.01")


def test_genuinely_changed_price_is_rejected_and_reserves_nothing():
    checkout = service()
    with pytest.raises(ValueError, match="price changed"):
        checkout.purchase([Line("A", "9.99", 1)])
    assert checkout.inventory.available("A") == 5


def test_later_price_mismatch_is_rejected_before_any_reservation():
    class RecordingInventory(Inventory):
        def __init__(self):
            super().__init__({"A": 5, "B": 3})
            self.reservation_calls = []

        def reserve(self, sku, quantity):
            self.reservation_calls.append((sku, quantity))
            return super().reserve(sku, quantity)

        def reserve_many(self, quantities):
            self.reservation_calls.append(("many", dict(quantities)))
            return super().reserve_many(quantities)

    inventory = RecordingInventory()
    checkout = Checkout(inventory, {"A": "10.005", "B": "2.50"})
    with pytest.raises(ValueError, match="price changed"):
        checkout.purchase([Line("A", "10.005", 1), Line("B", "9.99", 1)])
    assert inventory.reservation_calls == []
    assert inventory.available("A") == 5
    assert inventory.available("B") == 3


def test_rounds_catalog_price_after_multiplying_each_line():
    receipt = service().purchase([Line("A", "10.005", 2), Line("B", "2.50", 1)])
    assert receipt.subtotal == Decimal("22.51")  # round(10.005 * 2) + 2.50


def test_duplicate_skus_are_accounted_for_together():
    checkout = service({"A": 5})
    receipt = checkout.purchase([Line("A", "10.005", 3), Line("A", "10.005", 2)])
    assert receipt.subtotal == Decimal("50.03")  # rounds per line: 30.02 + 20.01
    assert checkout.inventory.available("A") == 0


def test_duplicate_sku_failure_releases_all_reservations():
    checkout = service({"A": 3})
    with pytest.raises(ValueError, match="insufficient stock"):
        checkout.purchase([Line("A", "10.005", 2), Line("A", "10.005", 2)])
    assert checkout.inventory.available("A") == 3


def test_invalid_discount_releases_multiple_sku_reservations():
    checkout = service({"A": 2, "B": 2})
    with pytest.raises(ValueError, match="between"):
        checkout.purchase([Line("A", "10.005", 1), Line("B", "2.50", 1)], "101")
    assert checkout.inventory.available("A") == 2
    assert checkout.inventory.available("B") == 2


def test_unknown_sku_releases_nothing():
    checkout = service()
    with pytest.raises(KeyError):
        checkout.purchase([Line("A", "10.005", 1), Line("Z", "1.00", 1)])
    assert checkout.inventory.available("A") == 5


def test_concurrent_purchases_never_oversell():
    stock = 20
    inventory = Inventory({"A": stock})
    successes = []
    lock = threading.Lock()

    def buy():
        checkout = Checkout(inventory, {"A": "1.00"})
        try:
            checkout.purchase([Line("A", "1.00", 1), Line("A", "1.00", 1)])
            with lock:
                successes.append(2)
        except ValueError:
            pass

    threads = [threading.Thread(target=buy) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    sold = sum(successes)
    assert sold <= stock
    assert inventory.available("A") == stock - sold


def test_half_up_rounding_primitives():
    assert money("2.675") == Decimal("2.68")
    assert line_total("0.125", 1) == Decimal("0.13")
    assert discount_total(Decimal("0.10"), "33.33") == Decimal("0.03")
    assert discount_total(Decimal("15.01"), "10") == Decimal("1.50")
