from decimal import Decimal

import pytest

from checkout import Checkout, Line
from inventory import Inventory


def service(stock=None):
    return Checkout(Inventory(stock or {"A": 5, "B": 3}), {"A": "10.005", "B": "2.50"})


def test_purchase_rounds_each_line_and_discount():
    receipt = service().purchase([Line("A", "10.005", 1), Line("B", "2.50", 2)], "10")
    assert receipt.subtotal == Decimal("15.01")
    assert receipt.discount == Decimal("1.50")
    assert receipt.total == Decimal("13.51")


def test_failed_purchase_releases_every_reservation():
    checkout = service({"A": 2, "B": 1})
    with pytest.raises(ValueError, match="insufficient stock"):
        checkout.purchase([Line("A", "10.005", 2), Line("B", "2.50", 2)])
    assert checkout.inventory.available("A") == 2
    assert checkout.inventory.available("B") == 1


def test_commit_reduces_stock_and_rejects_invalid_discount():
    checkout = service()
    receipt = checkout.purchase([Line("A", "10.005", 2)], "25")
    assert receipt.total == Decimal("15.01")
    assert checkout.inventory.available("A") == 3
    with pytest.raises(ValueError, match="between"):
        checkout.purchase([Line("A", "10.005", 1)], "101")
    assert checkout.inventory.available("A") == 3
