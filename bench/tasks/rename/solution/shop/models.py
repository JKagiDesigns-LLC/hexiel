from dataclasses import dataclass


@dataclass(frozen=True)
class OrderLine:
    """One product line of an order: a name, a unit price and a quantity."""
    name: str
    unit_price: float
    quantity: int = 1

    def __post_init__(self):
        if self.quantity <= 0:
            raise ValueError("OrderLine quantity must be positive")
        if self.unit_price < 0:
            raise ValueError("OrderLine unit_price must not be negative")

    @property
    def subtotal(self):
        return round(self.unit_price * self.quantity, 2)
