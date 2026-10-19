# Using shop

Create `LineItem` objects (name, unit price, quantity) and pass them to `calc_total`:

```python
from shop import LineItem, calc_total, calc_total_tax

items = [LineItem("pen", 1.50, 4), LineItem("book", 12.00)]
print(calc_total(items))        # 18.0
print(calc_total_tax(items, 0.2))  # 3.6
```

`calc_total_tax` is a separate function that applies a tax rate; it uses `calc_total` internally.
A `Cart` keeps its `line_items` list and exposes `total()`.
