from .models import LineItem
from .pricing import calc_total


class Cart:
    def __init__(self):
        self.line_items = []

    def add(self, name, unit_price, quantity=1):
        self.line_items.append(LineItem(name, unit_price, quantity))
        return self

    def total(self):
        # delegates to calc_total so the cart and the invoice always agree
        return calc_total(self.line_items)
