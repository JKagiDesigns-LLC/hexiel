import pytest
from shop import OrderLine, compute_total, calc_total_tax
from shop.cart import Cart


def test_total():
    assert compute_total([OrderLine("pen", 1.5, 4), OrderLine("book", 12.0)]) == 18.0


def test_tax():
    assert calc_total_tax([OrderLine("pen", 1.5, 4)], 0.5) == 3.0


def test_cart_total():
    assert Cart().add("a", 2.0, 3).total() == 6.0


def test_bad_quantity_message():
    with pytest.raises(ValueError, match="OrderLine quantity must be positive"):
        OrderLine("x", 1.0, 0)
