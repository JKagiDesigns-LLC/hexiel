from .models import OrderLine


def compute_total(items):
    """Sum of the subtotals of all OrderLine objects, rounded to cents."""
    total = 0.0
    for item in items:
        if not isinstance(item, OrderLine):
            raise TypeError(f"expected OrderLine, got {type(item).__name__}")
        total += item.subtotal
    return round(total, 2)


def calc_total_tax(items, rate):
    """Tax owed on the order: compute_total(items) * rate, rounded to cents."""
    return round(compute_total(items) * rate, 2)
