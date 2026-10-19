# Using shop

Create `OrderLine` objects (name, unit price, quantity) and pass them to `compute_total`:

```python
from shop import OrderLine, compute_total, calc_total_tax

items = [OrderLine("pen", 1.50, 4), OrderLine("book", 12.00)]
print(compute_total(items))        # 18.0
print(calc_total_tax(items, 0.2))  # 3.6
```

`calc_total_tax` is a separate function that applies a tax rate; it uses `compute_total` internally.
A `Cart` keeps its `line_items` list and exposes `total()`.
