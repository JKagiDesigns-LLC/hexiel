from .models import LineItem


def describe(item: LineItem) -> str:
    """Human readable line used by the report: 'name (qty)'. See docs/USAGE.md for LineItem."""
    return f"{item.name} ({item.quantity})"
