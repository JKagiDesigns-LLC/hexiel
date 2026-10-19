# stats.py

`python stats.py FILE [--group-by COL] [--agg FUNC:COL ...] [--sort KEY]`

Reads a CSV file (first row = header) and prints summary statistics as CSV on stdout.

## Aggregations
`--agg` takes one or more `FUNC:COL` items. `FUNC` is `sum`, `avg`, `min`, `max` or `count`.
Each produces an output column named `FUNC(COL)`, e.g. `sum(amount)`, in the order given.
If no `--agg` is given, the default is a single `count(*)` column that counts rows.
* Empty cells are ignored (`count(COL)` counts non-empty cells; `count(*)` counts all rows).
* For `sum`, `avg`, `min` and `max` all non-empty cells of the column must be numbers (`int` or `float` syntax); see Errors.
  `count:COL` works on any column, numeric or not: it just counts the non-empty cells.
* `sum`, `min`, `max` print as an integer when the result is whole (`12`, not `12.0`), otherwise with exactly 2 decimals.
  `avg` always prints exactly 2 decimals. `count` prints an integer.
* If every cell in the group is empty, `sum` and `count` print `0`, and `avg`, `min` and `max` print an empty cell.

## Grouping
Without `--group-by` there is exactly one output row. With `--group-by COL`, rows are grouped by the cell value of
`COL` (an empty cell is its own group, shown as `(none)`). The first output column is `COL`, then the aggregate columns.

## Sorting
Groups are sorted by group value, ascending, comparing as plain strings; `(none)` sorts last.
`--sort KEY` sorts by an output column instead: `KEY` is the column name, e.g. `sum(amount)`; a leading `-` means descending.
Both `--sort KEY` and `--sort=KEY` must work, including keys that start with `-` (`--sort -sum(amount)`).
Numeric columns compare numerically, with empty cells last; ties are broken by group value ascending.

## Output
First line is the header, then one line per row. Cells are separated by `,` (no spaces, no quoting needed for this exercise).

Example for `sales.csv` below:

    $ python stats.py sales.csv --group-by region --agg sum:amount avg:amount
    region,sum(amount),avg(amount)
    east,30,15.00
    north,17.50,8.75
    west,5,5.00

## Errors (message on stderr, nothing on stdout)
| situation | message | exit code |
|---|---|---|
| file cannot be read | `error: cannot read FILE` | 1 |
| unknown column in `--group-by`, `--agg` or `--sort` | `error: unknown column 'NAME'` | 2 |
| unknown function in `--agg` | `error: unknown function 'NAME'` | 2 |
| a non-number in an aggregated column | `error: row N: 'VALUE' is not a number (column 'COL')` | 3 |

`N` is the 1-based data row number (the first row after the header is row 1). Check order: file, then arguments
(group-by, aggs, sort), then values row by row.
