import sys

from .cart import Cart
from .invoice import render


def main(argv):
    cart = Cart()
    for spec in argv:
        name, price, qty = spec.split(":")
        cart.add(name, float(price), int(qty))
    print(render(cart))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
