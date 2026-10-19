import pathlib
import re

import pytest
from shop import OrderLine, compute_total, calc_total_tax
from shop.cart import Cart
from shop.invoice import render
from shop.report import describe

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_new_names_work():
    items = [OrderLine("pen", 1.5, 4), OrderLine("book", 12.0)]
    assert compute_total(items) == 18.0
    assert calc_total_tax(items, 0.2) == 3.6
    assert Cart().add("a", 2.0, 3).total() == 6.0
    assert describe(OrderLine("pen", 1.0, 2)) == "pen (2)"
    assert "TAX" in render(Cart().add("a", 10.0, 1), 0.1)


def test_message_was_renamed():
    with pytest.raises(ValueError, match="OrderLine quantity must be positive"):
        OrderLine("x", 1.0, 0)
    with pytest.raises(TypeError, match="expected OrderLine, got str"):
        compute_total(["nope"])


def test_old_names_are_gone_everywhere():
    offenders = []
    for path in list(ROOT.rglob("*.py")) + list(ROOT.rglob("*.md")):
        if any(part.startswith(".") or part in ("hidden_tests", "__pycache__") for part in path.relative_to(ROOT).parts):
            continue                               # .git, .hexiel (hexiel's own notes), caches, the hidden tests
        text = path.read_text()
        if re.search(r"\bLineItem\b", text) or re.search(r"\bcalc_total\b", text):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, offenders


def test_look_alikes_were_left_alone():
    pricing = (ROOT / "shop" / "pricing.py").read_text()
    assert "def calc_total_tax(" in pricing
    assert "line_items" in (ROOT / "shop" / "cart.py").read_text()
    docs = (ROOT / "docs" / "USAGE.md").read_text()
    assert "calc_total_tax" in docs and "OrderLine" in docs and "compute_total" in docs
    assert "compute_total(items) * rate" in pricing or "compute_total(items)" in pricing


def test_package_exports():
    import shop
    assert set(shop.__all__) == {"OrderLine", "compute_total", "calc_total_tax"}
