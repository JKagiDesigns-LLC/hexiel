from .models import OrderLine
from .pricing import compute_total


class Cart:
    def __init__(self):
        self.line_items = []

    def add(self, name, unit_price, quantity=1):
        self.line_items.append(OrderLine(name, unit_price, quantity))
        return self

    def total(self):
        # delegates to compute_total so the cart and the invoice always agree
        return compute_total(self.line_items)
