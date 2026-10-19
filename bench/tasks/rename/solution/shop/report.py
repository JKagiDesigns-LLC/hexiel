from .models import OrderLine


def describe(item: OrderLine) -> str:
    """Human readable line used by the report: 'name (qty)'. See docs/USAGE.md for OrderLine."""
    return f"{item.name} ({item.quantity})"
