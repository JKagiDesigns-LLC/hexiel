from .models import LineItem


def calc_total(items):
    """Sum of the subtotals of all LineItem objects, rounded to cents."""
    total = 0.0
    for item in items:
        if not isinstance(item, LineItem):
            raise TypeError(f"expected LineItem, got {type(item).__name__}")
        total += item.subtotal
    return round(total, 2)


def calc_total_tax(items, rate):
    """Tax owed on the order: calc_total(items) * rate, rounded to cents."""
    return round(calc_total(items) * rate, 2)
