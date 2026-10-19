import pytest
from shop import LineItem, calc_total, calc_total_tax
from shop.cart import Cart


def test_total():
    assert calc_total([LineItem("pen", 1.5, 4), LineItem("book", 12.0)]) == 18.0


def test_tax():
    assert calc_total_tax([LineItem("pen", 1.5, 4)], 0.5) == 3.0


def test_cart_total():
    assert Cart().add("a", 2.0, 3).total() == 6.0


def test_bad_quantity_message():
    with pytest.raises(ValueError, match="LineItem quantity must be positive"):
        LineItem("x", 1.0, 0)
