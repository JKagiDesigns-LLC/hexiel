"""Deterministic helpers: exact answers that models get wrong when they do them "in their head" — date math, unit
conversion, regex testing, JSON paths, CSV column stats, hashes, base64, semver and arithmetic. One `util` tool,
pure Python, no network, no model."""

from __future__ import annotations

import ast
import base64
import csv
import datetime as dt
import hashlib
import io
import json
import math
import operator
import re
import statistics
from pathlib import Path


class HelperError(ValueError):
    pass


# ---------------------------------------------------------------- arithmetic

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg,
        ast.UAdd: operator.pos}
_FUNCS = {n: getattr(math, n) for n in ("sqrt", "log", "log2", "log10", "exp", "sin", "cos", "tan", "floor", "ceil",
                                        "factorial", "gcd", "radians", "degrees", "hypot")}
_FUNCS.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum})
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def calc(expr: str):
    """Arithmetic with + - * / // % ** and math functions — evaluated over the AST, never eval()."""
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10_000:
                raise HelperError("exponent too large")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        if isinstance(node, (ast.List, ast.Tuple)):
            return [ev(e) for e in node.elts]
        raise HelperError(f"not allowed in a calculation: {ast.dump(node)[:60]}")
    try:
        return ev(ast.parse(expr.replace("^", "**").replace("×", "*").replace("÷", "/"), mode="eval"))
    except SyntaxError as exc:
        raise HelperError(f"cannot parse {expr!r}: {exc.msg}") from None


# ---------------------------------------------------------------- dates

_UNITS_T = {"second": "seconds", "sec": "seconds", "minute": "minutes", "min": "minutes", "hour": "hours", "h": "hours",
            "day": "days", "d": "days", "week": "weeks", "w": "weeks", "month": "months", "year": "years", "y": "years"}


def _parse_date(text: str) -> dt.datetime:
    text = text.strip()
    if text.lower() in ("today", "now"):
        return dt.datetime.now().replace(microsecond=0) if text.lower() == "now" else \
            dt.datetime.combine(dt.date.today(), dt.time())
    if re.fullmatch(r"\d{10}(\.\d+)?", text):
        return dt.datetime.fromtimestamp(float(text))
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d", "%d.%m.%Y",
                "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return dt.datetime.strptime(text.replace(",", ""), fmt)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise HelperError(f"unrecognised date {text!r} (use YYYY-MM-DD, ISO 8601, 'today' or a unix time)") from None


def _add_months(d: dt.datetime, months: int) -> dt.datetime:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    import calendar
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def date(expr: str) -> str:
    """'2026-11-11 + 45 days', 'today - 3 weeks', '2026-01-01 to 2026-11-11', 'weekday 2027-02-14', '1767225600'."""
    expr = expr.strip()
    m = re.fullmatch(r"(.+?)\s+(?:to|until|->|-)\s+(\d{4}-\d\d-\d\d.*|today|now)", expr, re.I)
    if m and not re.search(r"[-+]\s*\d+\s*[a-z]", expr[len(m.group(1)):], re.I):
        a, b = _parse_date(m.group(1)), _parse_date(m.group(2))
        delta = b - a
        days = delta.days + delta.seconds / 86400
        business = sum(1 for i in range(abs(delta.days)) if (min(a, b) + dt.timedelta(days=i)).weekday() < 5)
        return (f"{delta.days} days ({days / 7:.2f} weeks, {business} weekdays)"
                + (f", {delta.seconds // 3600}h {delta.seconds % 3600 // 60}m" if delta.seconds else ""))
    m = re.fullmatch(r"(?:weekday|day of week)\s+(.+)", expr, re.I)
    if m:
        d = _parse_date(m.group(1))
        return f"{d:%Y-%m-%d} is a {d:%A} (ISO week {d.isocalendar()[1]}, day {d.timetuple().tm_yday} of the year)"
    parts = re.split(r"\s*([+-])\s*(?=\d+\s*[a-zA-Z])", expr)
    base = _parse_date(parts[0])
    for sign, amount in zip(parts[1::2], parts[2::2]):
        for num, unit in re.findall(r"(\d+)\s*([a-z]+)", amount, re.I):
            key = _UNITS_T.get(unit.lower().rstrip("s"), _UNITS_T.get(unit.lower()))
            if not key:
                raise HelperError(f"unknown unit {unit!r}")
            n = int(num) * (1 if sign == "+" else -1)
            if key == "months":
                base = _add_months(base, n)
            elif key == "years":
                base = _add_months(base, 12 * n)
            else:
                base = base + dt.timedelta(**{key: n})
    has_time = base.time() != dt.time()
    return f"{base:%Y-%m-%d %H:%M:%S}" if has_time else f"{base:%Y-%m-%d} ({base:%A})" + \
        (f", unix {int(base.timestamp())}" if re.fullmatch(r"\d{10}", parts[0].strip()) else "")


# ---------------------------------------------------------------- units

_LEN = {"m": 1, "km": 1000, "cm": 0.01, "mm": 0.001, "um": 1e-6, "nm": 1e-9, "in": 0.0254, "inch": 0.0254, "ft": 0.3048,
        "foot": 0.3048, "feet": 0.3048, "yd": 0.9144, "mi": 1609.344, "mile": 1609.344, "nmi": 1852}
_MASS = {"kg": 1, "g": 0.001, "mg": 1e-6, "t": 1000, "lb": 0.45359237, "lbs": 0.45359237, "oz": 0.028349523125}
_VOL = {"l": 1, "ml": 0.001, "m3": 1000, "gal": 3.785411784, "qt": 0.946352946, "pt": 0.473176473, "cup": 0.2365882365,
        "floz": 0.0295735295625, "tbsp": 0.01478676478125, "tsp": 0.00492892159375}
_DATA = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12, "pb": 1e15, "kib": 1024, "mib": 1024 ** 2,
         "gib": 1024 ** 3, "tib": 1024 ** 4, "bit": 0.125, "kbit": 125, "mbit": 125e3, "gbit": 125e6}
_TIME = {"s": 1, "ms": 0.001, "us": 1e-6, "ns": 1e-9, "min": 60, "h": 3600, "hr": 3600, "day": 86400, "week": 604800,
         "year": 31557600}
_SPEED = {"m/s": 1, "km/h": 1 / 3.6, "kmh": 1 / 3.6, "mph": 0.44704, "knot": 0.514444, "kn": 0.514444, "ft/s": 0.3048}
_AREA = {"m2": 1, "km2": 1e6, "cm2": 1e-4, "ft2": 0.09290304, "sqft": 0.09290304, "acre": 4046.8564224, "ha": 1e4}
_TABLES = [_LEN, _MASS, _VOL, _DATA, _TIME, _SPEED, _AREA]
_TEMP = {"c", "f", "k", "°c", "°f", "celsius", "fahrenheit", "kelvin"}


def units(expr: str) -> str:
    """'5 mi to km', '72 F to C', '3.5 GiB in MB', '60 mph to m/s'."""
    m = re.fullmatch(r"\s*(-?[\d.,_]+(?:e-?\d+)?)\s*([^\s]+)\s+(?:to|in|->|as)\s+([^\s]+)\s*", expr, re.I)
    if not m:
        raise HelperError("use the form '<number> <unit> to <unit>', e.g. '5 mi to km'")
    value = float(m.group(1).replace(",", "").replace("_", ""))
    src, dst = m.group(2).lower(), m.group(3).lower()
    if src in _TEMP and dst in _TEMP:
        to_c = {"c": lambda v: v, "f": lambda v: (v - 32) * 5 / 9, "k": lambda v: v - 273.15}
        from_c = {"c": lambda v: v, "f": lambda v: v * 9 / 5 + 32, "k": lambda v: v + 273.15}
        s, d = src.strip("°")[0], dst.strip("°")[0]
        out = from_c[d](to_c[s](value))
        return f"{value:g} {m.group(2)} = {out:.6g} {m.group(3)}"
    for table in _TABLES:
        if src in table and dst in table:
            out = value * table[src] / table[dst]
            return f"{value:g} {m.group(2)} = {out:.10g} {m.group(3)}"
    raise HelperError(f"can't convert {src} to {dst} (unknown or incompatible units)")


# ---------------------------------------------------------------- regex, json, csv, hash, base64, semver

def regex(pattern: str, text: str, flags: str = "") -> str:
    fl = sum({"i": re.I, "m": re.M, "s": re.S, "x": re.X}.get(c, 0) for c in flags)
    try:
        rx = re.compile(pattern, fl)
    except re.error as exc:
        raise HelperError(f"bad regex: {exc}") from None
    found = list(rx.finditer(text))
    if not found:
        return "no match"
    lines = [f"{len(found)} match(es):"]
    for n, mt in enumerate(found[:50], 1):
        groups = "" if not mt.groups() else f"  groups={list(mt.groups())}"
        named = "" if not mt.groupdict() else f"  named={mt.groupdict()}"
        lines.append(f"{n}. [{mt.start()}:{mt.end()}] {mt.group(0)!r}{groups}{named}")
    return "\n".join(lines)


def json_path(path: str, data) -> str:
    """'a.b[0].c', 'items[*].name', 'items[-1]'; data is JSON text or a parsed value."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError as exc:
            raise HelperError(f"not valid JSON: {exc}") from None
    tokens = re.findall(r"\[(\*|-?\d+|'[^']*'|\"[^\"]*\")\]|([^.\[\]]+)", path.lstrip("$."))
    nodes = [data]
    for idx, key in tokens:
        nxt = []
        for node in nodes:
            if idx == "*":
                nxt.extend(node if isinstance(node, list) else list(node.values()) if isinstance(node, dict) else [])
            elif idx:
                k = idx.strip("'\"")
                if isinstance(node, list) and re.fullmatch(r"-?\d+", k):
                    i = int(k)
                    if -len(node) <= i < len(node):
                        nxt.append(node[i])
                elif isinstance(node, dict) and k in node:
                    nxt.append(node[k])
            elif isinstance(node, dict) and key in node:
                nxt.append(node[key])
            elif isinstance(node, list) and key == "length":
                nxt.append(len(node))
        nodes = nxt
    if not nodes:
        return "no value at that path"
    out = nodes[0] if len(nodes) == 1 and "*" not in path else nodes
    return json.dumps(out, indent=2, ensure_ascii=False)[:20_000]


def csv_stats(path: Path, max_rows: int = 2_000_000) -> str:
    """Rows, columns and per-column type / empties / distinct / min / max / mean — for a quick look at a CSV."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        sample = fh.read(65536)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        header = next(reader, [])
        cols = [[] for _ in header]
        rows = 0
        for row in reader:
            rows += 1
            if rows > max_rows:
                break
            for i, cell in enumerate(row[:len(header)]):
                cols[i].append(cell)
    lines = [f"{path.name}: {rows} rows × {len(header)} columns (delimiter {getattr(dialect, 'delimiter', ',')!r})"]
    for name, values in zip(header, cols):
        filled = [v for v in values if v.strip()]
        nums = []
        for v in filled:
            try:
                nums.append(float(v.replace(",", "")) if re.fullmatch(r"-?[\d,]*\.?\d+(e-?\d+)?", v.strip()) else None)
            except ValueError:
                nums.append(None)
        numeric = filled and all(n is not None for n in nums)
        empty = len(values) - len(filled)
        distinct = len(set(filled))
        if numeric:
            vals = [n for n in nums if n is not None]
            lines.append(f"- {name}: number, min {min(vals):g}, max {max(vals):g}, mean {statistics.fmean(vals):.4g}, "
                         f"median {statistics.median(vals):g}, {empty} empty")
        else:
            top = max(set(filled), key=filled.count) if filled and distinct <= 5000 else ""
            if top and filled.count(top) < 2:
                top = ""                          # every value unique: there is no "most common"
            lines.append(f"- {name}: text, {distinct} distinct, {empty} empty"
                         + (f", most common {top[:40]!r}" if top else ""))
    return "\n".join(lines)


def hash_of(algo: str, data: bytes) -> str:
    algo = (algo or "sha256").lower().replace("-", "")
    if algo not in hashlib.algorithms_available:
        raise HelperError(f"unknown hash {algo!r}; try sha256, sha1, md5, sha512, blake2b")
    return f"{algo} {hashlib.new(algo, data).hexdigest()}"  # nosec B324 - checksums on request, not security


def b64(op: str, text: str) -> str:
    if op == "decode":
        try:
            raw = base64.b64decode(text.strip() + "=" * (-len(text.strip()) % 4), altchars=b"-_" if "-" in text or "_" in text else None)
        except (ValueError, base64.binascii.Error) as exc:
            raise HelperError(f"not base64: {exc}") from None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"({len(raw)} bytes, not text) hex: {raw[:64].hex()}{'…' if len(raw) > 64 else ''}"
    return base64.b64encode(text.encode()).decode()


_SEMVER = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def _ver(v: str) -> tuple:
    m = _SEMVER.match(v.strip())
    if not m:
        raise HelperError(f"not a version: {v!r}")
    pre = m.group(4)
    pre_key = (1,) if pre is None else (0, *[(0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")])
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0), pre_key)


def _satisfies(v: tuple, rng: str) -> bool:
    for alt in rng.split("||"):
        ok = True
        for part in alt.split():
            m = re.match(r"^(\^|~|>=|<=|>|<|=)?(.+)$", part)
            op, ver = m.group(1) or "=", m.group(2)
            base = _ver(ver.replace("x", "0").replace("*", "0"))
            if op == "^":
                upper = (base[0] + 1, 0, 0, (0,)) if base[0] else ((0, base[1] + 1, 0, (0,)) if base[1] else (0, 0, base[2] + 1, (0,)))
                ok &= base <= v < upper
            elif op == "~":
                ok &= base <= v < (base[0], base[1] + 1, 0, (0,))
            else:
                ok &= {"=": v == base, ">=": v >= base, "<=": v <= base, ">": v > base, "<": v < base}[op]
        if ok:
            return True
    return False


def semver(expr: str) -> str:
    """'1.2.3 < 1.10.0', 'sort 1.10.0 1.2.0 1.2.0-rc1', 'bump minor 1.4.2', '1.5.0 satisfies ^1.2'."""
    e = expr.strip()
    m = re.fullmatch(r"bump\s+(major|minor|patch)\s+(\S+)", e, re.I)
    if m:
        a, b, c, _ = _ver(m.group(2))
        kind = m.group(1).lower()
        return {"major": f"{a + 1}.0.0", "minor": f"{a}.{b + 1}.0", "patch": f"{a}.{b}.{c + 1}"}[kind]
    m = re.fullmatch(r"sort\s+(.+)", e, re.I)
    if m:
        return " < ".join(sorted(m.group(1).replace(",", " ").split(), key=_ver))
    m = re.fullmatch(r"(\S+)\s+(?:satisfies|in|matches)\s+(.+)", e, re.I)
    if m:
        return "yes" if _satisfies(_ver(m.group(1)), m.group(2)) else "no"
    m = re.fullmatch(r"(\S+)\s*(<=|>=|==|<|>|=|vs)\s*(\S+)", e)
    if m:
        a, b = _ver(m.group(1)), _ver(m.group(3))
        rel = "<" if a < b else ">" if a > b else "=="
        if m.group(2) == "vs":
            return f"{m.group(1)} {rel} {m.group(3)}"
        op = m.group(2).replace("==", "=")
        truth = {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b, "=": a == b}[op]
        return f"{'true' if truth else 'false'} ({m.group(1)} {rel} {m.group(3)})"
    raise HelperError("use: 'A < B', 'A vs B', 'sort A B C', 'bump minor A', 'A satisfies ^1.2'")


OPS = ("calc", "date", "units", "regex", "json", "csv", "hash", "base64", "semver")


def run(op: str, args: dict, cwd: str) -> str:
    op = (op or "").lower().strip()
    expr = str(args.get("expr") or args.get("input") or "")

    def text_or_file() -> bytes:
        if args.get("path"):
            p = Path(args["path"]) if Path(args["path"]).is_absolute() else Path(cwd) / args["path"]
            return p.read_bytes()
        return str(args.get("text") or expr).encode()

    if op == "calc":
        value = calc(expr)
        return f"{expr} = {value:.12g}" if isinstance(value, float) else f"{expr} = {value}"
    if op == "date":
        return date(expr)
    if op == "units":
        return units(expr)
    if op == "regex":
        return regex(str(args.get("pattern") or expr), text_or_file().decode("utf-8", "replace") if args.get("path")
                     else str(args.get("text", "")), str(args.get("flags", "")))
    if op == "json":
        return json_path(str(args.get("pattern") or expr or "$"), text_or_file().decode("utf-8", "replace"))
    if op == "csv":
        if not args.get("path"):
            raise HelperError("csv needs path=<file>")
        p = Path(args["path"]) if Path(args["path"]).is_absolute() else Path(cwd) / args["path"]
        return csv_stats(p)
    if op == "hash":
        return hash_of(str(args.get("algo") or "sha256"), text_or_file())
    if op == "base64":
        return b64("decode" if str(args.get("mode", "encode")).startswith("d") else "encode",
                   text_or_file().decode("utf-8", "replace"))
    if op == "semver":
        return semver(expr)
    raise HelperError(f"unknown op {op!r}; one of: {', '.join(OPS)}")
