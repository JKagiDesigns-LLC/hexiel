"""CSV statistics tool. See README.md."""
import argparse
import csv
import sys

FUNCS = ("sum", "avg", "min", "max", "count")


class Fail(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


def num(text):
    try:
        return int(text)
    except ValueError:
        return float(text)


def fmt_whole_or_2(x):
    return str(int(x)) if float(x).is_integer() else f"{x:.2f}"


def aggregate(func, col, rows):
    if col == "*":
        return str(len(rows))
    vals = [r[col] for r in rows if r[col] != ""]
    if func == "count":
        return str(len(vals))
    nums = [num(v) for v in vals]
    if not nums:
        return "0" if func == "sum" else ""
    if func == "sum":
        return fmt_whole_or_2(sum(nums))
    if func == "avg":
        return f"{sum(nums) / len(nums):.2f}"
    if func == "min":
        return fmt_whole_or_2(min(nums))
    return fmt_whole_or_2(max(nums))


def run(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--group-by")
    ap.add_argument("--agg", nargs="+", default=[])
    ap.add_argument("--sort")
    fixed, i = [], 0
    while i < len(argv):                      # argparse mistakes "-sum(x)" for an option: glue --sort and its value together
        if argv[i] == "--sort" and i + 1 < len(argv):
            fixed.append("--sort=" + argv[i + 1])
            i += 2
        else:
            fixed.append(argv[i])
            i += 1
    a = ap.parse_args(fixed)
    try:
        with open(a.file, newline="") as fh:
            reader = csv.DictReader(fh)
            header = reader.fieldnames or []
            data = list(reader)
    except OSError:
        raise Fail(f"error: cannot read {a.file}", 1)
    aggs = []
    for item in a.agg or ["count:*"]:
        func, _, col = item.partition(":")
        aggs.append((func, col))
    if a.group_by and a.group_by not in header:
        raise Fail(f"error: unknown column '{a.group_by}'", 2)
    for func, col in aggs:
        if col != "*" and col not in header:
            raise Fail(f"error: unknown column '{col}'", 2)
        if func not in FUNCS:
            raise Fail(f"error: unknown function '{func}'", 2)
    names = [f"{f}({c})" for f, c in aggs]
    out_cols = ([a.group_by] if a.group_by else []) + names
    key = a.sort.lstrip("-") if a.sort else None
    if key and key not in out_cols:
        raise Fail(f"error: unknown column '{key}'", 2)
    for i, row in enumerate(data, 1):
        for func, col in aggs:
            if col != "*" and func != "count" and row[col] != "":
                try:
                    num(row[col])
                except ValueError:
                    raise Fail(f"error: row {i}: '{row[col]}' is not a number (column '{col}')", 3)
    groups = {}
    for row in data:
        groups.setdefault(row[a.group_by] if a.group_by else None, []).append(row)
    if not a.group_by:
        groups = {None: data}
    rows = []
    for g, members in groups.items():
        cells = ([g if g != "" else "(none)"] if a.group_by else [])
        cells += [aggregate(f, c, members) for f, c in aggs]
        rows.append((g, cells))

    def gkey(item):
        g = item[0]
        return (g == "", g or "")
    rows.sort(key=gkey)
    if key:
        idx = out_cols.index(key)
        desc = a.sort.startswith("-")

        def skey(item):
            cell = item[1][idx]
            try:
                n = float(cell)
                return (0, -n if desc else n, gkey(item))
            except ValueError:
                return (0, cell, gkey(item)) if a.group_by and idx == 0 else (1, 0, gkey(item))
        if a.group_by and idx == 0:
            rows.sort(key=lambda it: it[1][0], reverse=desc)
            if not desc:
                rows.sort(key=lambda it: (it[1][0] == "(none)", it[1][0]))
        else:
            rows.sort(key=skey)
    return [",".join(out_cols)] + [",".join(c) for _, c in rows]


def main(argv):
    try:
        lines = run(argv)
    except Fail as e:
        print(e, file=sys.stderr)
        return e.code
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
