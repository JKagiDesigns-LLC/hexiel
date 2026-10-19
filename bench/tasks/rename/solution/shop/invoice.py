from .pricing import compute_total, calc_total_tax


def render(cart, tax_rate=0.0):
    lines = [f"{i.quantity} x {i.name} @ {i.unit_price:.2f} = {i.subtotal:.2f}" for i in cart.line_items]
    lines.append(f"TOTAL {compute_total(cart.line_items):.2f}")
    if tax_rate:
        lines.append(f"TAX {calc_total_tax(cart.line_items, tax_rate):.2f}")
    return "\n".join(lines)
