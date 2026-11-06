"""Data tools for analysts and researchers — the model asks, Python does the work.

  data        schema cards + SQL over CSV/TSV/Excel/Parquet/JSON files (DuckDB)
  data_export save a query result as CSV / XLSX / JSON / Parquet
  db_query    read a local SQLite database (tables + SELECT), read-only
  db_execute  create tables / insert / update a local SQLite database
  sheet_read  read cells/ranges of an .xlsx workbook
  sheet_write write cells / append rows / add sheets, keeping formatting
  chart       interactive HTML chart (Vega-Lite) from a query or rows

Token economy: the model never receives raw files. It gets a ~300-token schema
card per table (DuckDB SUMMARIZE + a few sample rows), writes SQL, and only the
aggregated result (≤ 50 rows) comes back."""

import csv
import json
import re
import sqlite3
from pathlib import Path

from .tools import ToolDef, ToolResult, _resolve_path, text_result

MAX_ROWS = 50
TABLE_EXTS = {".csv": "csv", ".tsv": "csv", ".txt": "csv", ".parquet": "parquet",
              ".json": "json", ".jsonl": "json", ".ndjson": "json", ".xlsx": "xlsx", ".xlsm": "xlsx"}


def _duckdb():
    try:
        import duckdb
        return duckdb
    except ImportError as exc:
        raise RuntimeError("the data tools need duckdb: pip install duckdb") from exc


def _ident(name: str) -> str:
    name = re.sub(r"\W+", "_", name).strip("_").lower() or "t"
    return f"t_{name}" if name[0].isdigit() else name


def _xlsx_to_csvs(path: Path, ctx) -> list[tuple[str, Path]]:
    """Each worksheet → cached CSV (pure-Python openpyxl; no network-installed extensions)."""
    import openpyxl
    cache = Path(ctx.cwd) / ".hexiel" / "datacache"
    cache.mkdir(parents=True, exist_ok=True)
    stamp = f"{path.stat().st_mtime_ns}"
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        target = cache / f"{_ident(path.stem)}__{_ident(ws.title)}__{stamp}.csv"
        if not target.exists():
            with target.open("w", newline="") as fh:
                writer = csv.writer(fh)
                for row in ws.iter_rows(values_only=True):
                    if any(v is not None for v in row):
                        writer.writerow(["" if v is None else v for v in row])
        name = _ident(path.stem) if len(wb.worksheets) == 1 else f"{_ident(path.stem)}_{_ident(ws.title)}"
        out.append((name, target))
    wb.close()
    return out


def _connect(files: list[str], ctx):
    """DuckDB connection with one view per file (or per sheet); returns (con, [view names])."""
    duckdb = _duckdb()
    con = duckdb.connect()
    names = []
    for raw in files:
        path = _resolve_path(raw, ctx)
        kind = TABLE_EXTS.get(path.suffix.lower())
        if not path.is_file():
            raise FileNotFoundError(f"{raw}: file not found")
        if kind is None:
            raise ValueError(f"{raw}: unsupported type (csv, tsv, xlsx, parquet, json)")
        sources = _xlsx_to_csvs(path, ctx) if kind == "xlsx" else [(_ident(path.stem), path)]
        for name, src in sources:
            reader = {"csv": "read_csv_auto", "parquet": "read_parquet", "json": "read_json_auto",
                      "xlsx": "read_csv_auto"}[kind]
            con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM {reader}('{src.as_posix()}')")  # nosec B608
            names.append(name)
    return con, names


def _table(cols: list[str], rows: list, total: int | None = None) -> str:
    def cell(v):
        s = "" if v is None else str(v)
        return (s[:60] + "…") if len(s) > 60 else s
    lines = [" | ".join(cols), " | ".join("---" for _ in cols)]
    lines += [" | ".join(cell(v) for v in r) for r in rows]
    if total is not None and total > len(rows):
        lines.append(f"… {total - len(rows)} more row(s) — aggregate or LIMIT to see what matters")
    return "\n".join(lines)


def _schema_card(con, name: str) -> str:
    total = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]  # nosec B608
    summary = con.execute(f"SUMMARIZE {name}").fetchall()
    cols = [d[0] for d in con.execute(f"SUMMARIZE {name}").description]
    idx = {c: i for i, c in enumerate(cols)}
    lines = [f"table {name}: {total:,} rows, {len(summary)} columns"]
    for r in summary:
        bits = [f"{r[idx['column_type']]}"]
        if r[idx["null_percentage"]] not in (None, 0, "0.0%", "0.00%"):
            bits.append(f"nulls {r[idx['null_percentage']]}")
        bits.append(f"~{r[idx['approx_unique']]} distinct")
        lo, hi = r[idx["min"]], r[idx["max"]]
        if lo is not None:
            bits.append(f"range {str(lo)[:24]} … {str(hi)[:24]}")
        lines.append(f"  - {r[idx['column_name']]}: " + ", ".join(bits))
    sample = con.execute(f"SELECT * FROM {name} LIMIT 5")  # nosec B608
    lines.append("sample rows:\n" + _table([d[0] for d in sample.description], sample.fetchall()))
    return "\n".join(lines)


# ---------------------------------------------------------------- data / export

def _data(args: dict, ctx) -> ToolResult:
    files = args.get("files") or ([args["file"]] if args.get("file") else [])
    if isinstance(files, str):
        files = [files]
    try:
        con, names = _connect(files, ctx)
        if args.get("action", "query") == "schema" or not args.get("sql"):
            if not names:
                return text_result("error: give `files` to describe", True)
            return text_result("\n\n".join(_schema_card(con, n) for n in names))
        sql = args["sql"].strip().rstrip(";")
        cur = con.execute(f"SELECT * FROM ({sql}) LIMIT {MAX_ROWS + 1}")  # nosec B608
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        total = len(rows)
        if total > MAX_ROWS:
            total = con.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0]  # nosec B608
            rows = rows[:MAX_ROWS]
        return text_result(_table(cols, rows, total) if rows else "(no rows)")
    except Exception as exc:  # noqa: BLE001 - SQL/file errors go back to the model to fix
        views = f" (tables: {', '.join(names)})" if "names" in locals() and names else ""
        return text_result(f"error: {exc}{views}", True)


def _data_export(args: dict, ctx) -> ToolResult:
    out = _resolve_path(args.get("out", ""), ctx)
    files = args.get("files") or []
    if isinstance(files, str):
        files = [files]
    try:
        con, _ = _connect(files, ctx)
        sql = args["sql"].strip().rstrip(";")
        out.parent.mkdir(parents=True, exist_ok=True)
        suffix = out.suffix.lower()
        if suffix in (".csv", ".parquet", ".json"):
            fmt = {".csv": "(HEADER, DELIMITER ',')", ".parquet": "(FORMAT PARQUET)", ".json": "(FORMAT JSON)"}[suffix]
            con.execute(f"COPY ({sql}) TO '{out.as_posix()}' {fmt}")
        elif suffix == ".xlsx":
            import openpyxl
            cur = con.execute(sql)
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append([d[0] for d in cur.description])
            for row in cur.fetchall():
                ws.append(list(row))
            wb.save(out)
        else:
            return text_result("error: out must end in .csv, .xlsx, .json or .parquet", True)
        n = con.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0]  # nosec B608
        return text_result(f"saved {n:,} row(s) to {out}")
    except Exception as exc:  # noqa: BLE001
        return text_result(f"error: {exc}", True)


# ---------------------------------------------------------------- SQLite

def _db_path(args, ctx) -> Path:
    return _resolve_path(args.get("path") or ".hexiel/data.db", ctx)


def _db_query(args: dict, ctx) -> ToolResult:
    path = _db_path(args, ctx)
    if not path.exists():
        return text_result(f"no database at {path} yet — create tables with db_execute", True)
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        sql = (args.get("sql") or "").strip()
        if not sql or args.get("action") == "tables":
            parts = []
            for name, ddl in con.execute("SELECT name, sql FROM sqlite_master WHERE type='table' "
                                         "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                n = con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]  # nosec B608
                parts.append(f"{ddl};  -- {n:,} row(s)")
            return text_result("\n".join(parts) or "(no tables yet)")
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
        more = len(rows) > MAX_ROWS
        return text_result(_table(cols, rows[:MAX_ROWS], MAX_ROWS + 1 if more else None) if cols else "ok")
    except sqlite3.Error as exc:
        return text_result(f"error: {exc} (this tool is read-only; use db_execute to change data)", True)


def _db_execute(args: dict, ctx) -> ToolResult:
    path = _db_path(args, ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        con = sqlite3.connect(path)
        rows = args.get("rows")
        if rows and args.get("sql"):               # parameterised bulk insert
            con.executemany(args["sql"], rows)
            changed = con.total_changes
        else:
            con.executescript(args.get("sql", ""))
            changed = con.total_changes
        con.commit()
        return text_result(f"ok — {changed} row(s) changed in {path}")
    except sqlite3.Error as exc:
        return text_result(f"error: {exc}", True)


# ---------------------------------------------------------------- spreadsheets

def _sheet_read(args: dict, ctx) -> ToolResult:
    import openpyxl
    path = _resolve_path(args["path"], ctx)
    try:
        wb = openpyxl.load_workbook(path, data_only=not args.get("formulas"))
    except (OSError, ValueError, KeyError) as exc:
        return text_result(f"error: {exc}", True)
    if not args.get("sheet") and not args.get("range"):
        info = [f"{ws.title}: {ws.max_row} rows × {ws.max_column} cols (used range {ws.dimensions})"
                for ws in wb.worksheets]
        return text_result("sheets:\n" + "\n".join(info))
    ws = wb[args["sheet"]] if args.get("sheet") else wb.active
    rng = args.get("range") or ws.dimensions
    cells = ws[rng]
    if not isinstance(cells, tuple):
        cells = ((cells,),)
    elif cells and not isinstance(cells[0], tuple):
        cells = (cells,)
    rows = [[c.value for c in row] for row in cells][:200]
    header = [f"{c.column_letter}" for c in cells[0]] if cells else []
    return text_result(f"{ws.title}!{rng}\n" + _table(header, rows))


def _sheet_write(args: dict, ctx) -> ToolResult:
    import openpyxl
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
    path = _resolve_path(args["path"], ctx)
    try:
        wb = openpyxl.load_workbook(path) if path.exists() else openpyxl.Workbook()
        name = args.get("sheet")
        if name and name not in wb.sheetnames:
            ws = wb.create_sheet(name)
        else:
            ws = wb[name] if name else wb.active
        values = args.get("values") or []
        if args.get("action") == "append":
            for row in values:
                ws.append(row if isinstance(row, list) else [row])
            where = f"appended {len(values)} row(s)"
        else:
            col_letter, row0 = coordinate_from_string(args.get("start", "A1"))
            col0 = column_index_from_string(col_letter)
            for i, row in enumerate(values):
                for j, v in enumerate(row if isinstance(row, list) else [row]):
                    ws.cell(row=row0 + i, column=col0 + j, value=v)   # keeps existing formatting
            where = f"wrote {sum(len(r) if isinstance(r, list) else 1 for r in values)} cell(s) at {args.get('start', 'A1')}"
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)
        return text_result(f"{where} in {path} [{ws.title}]")
    except (OSError, ValueError, KeyError) as exc:
        return text_result(f"error: {exc}", True)


# ---------------------------------------------------------------- charts

def _chart(args: dict, ctx) -> ToolResult:
    rows, cols = args.get("rows"), args.get("columns")
    if args.get("sql"):
        files = args.get("files") or []
        try:
            con, _ = _connect([files] if isinstance(files, str) else files, ctx)
            cur = con.execute(args["sql"])
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(5000)
        except Exception as exc:  # noqa: BLE001
            return text_result(f"error: {exc}", True)
    if not rows or not cols:
        return text_result("error: give sql (+ files) or rows + columns", True)
    values = [{c: (v if isinstance(v, (int, float, str)) or v is None else str(v))
               for c, v in zip(cols, r)} for r in rows]
    x, y = args.get("x") or cols[0], args.get("y") or cols[-1]
    kind = args.get("type", "bar")
    mark = {"bar": "bar", "line": "line", "scatter": "point", "area": "area", "pie": "arc"}.get(kind, "bar")

    def is_num(c):
        return all(isinstance(v.get(c), (int, float)) or v.get(c) is None for v in values[:200])
    if mark == "arc":
        encoding = {"theta": {"field": y, "type": "quantitative"}, "color": {"field": x, "type": "nominal"}}
    else:
        encoding = {"x": {"field": x, "type": "quantitative" if is_num(x) and mark in ("point",) else
                          ("temporal" if re.search(r"date|time|month|year|day", x, re.I) else "nominal"),
                          "sort": None},
                    "y": {"field": y, "type": "quantitative"}}
        if args.get("color"):
            encoding["color"] = {"field": args["color"], "type": "nominal"}
    spec = {"$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": args.get("title", ""), "width": "container", "height": 420,
            "data": {"values": values}, "mark": {"type": mark, "tooltip": True}, "encoding": encoding}
    out = _resolve_path(args.get("out") or f"charts/{_ident(args.get('title') or 'chart')}.html", ctx)
    out.parent.mkdir(parents=True, exist_ok=True)
    title = (args.get("title") or "chart").replace("<", "&lt;")
    out.write_text(f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/vega@5"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-lite@5"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-embed@6"></script>
<style>body{{font-family:system-ui,sans-serif;margin:24px}}#c{{width:100%}}</style></head>
<body><div id="c"></div><script>vegaEmbed('#c', {json.dumps(spec)});</script></body></html>""")
    return text_result(f"chart saved to {out} ({len(values)} point(s)) — open it in a browser")


# ---------------------------------------------------------------- registry

def _o(**props) -> dict:
    return {"type": "object", "properties": props}


S, A = {"type": "string"}, {"type": "array"}


def data_tools() -> list[ToolDef]:
    return [
        ToolDef("data", "Analyse CSV/TSV/Excel/Parquet/JSON files with SQL (DuckDB). action=schema "
                "returns a compact schema card per file (columns, types, ranges, sample rows) — call "
                "it first instead of reading the file. action=query runs `sql`; each file is a table "
                "named after it (sales.csv → sales; workbook sheets → book_sheet). Returns ≤50 rows: "
                "aggregate in SQL rather than pulling raw rows.",
                _o(action={"type": "string", "enum": ["schema", "query"]},
                   files={"type": "array", "items": S}, sql=S), readonly=True, run=_data),
        ToolDef("data_export", "Save a SQL query result over data files as .csv, .xlsx, .json or "
                ".parquet (e.g. a cleaned or summarised table for the user).",
                _o(files={"type": "array", "items": S}, sql=S, out=S), run=_data_export),
        ToolDef("db_query", "Read a local SQLite database (default .hexiel/data.db): action=tables "
                "lists tables with row counts, or run a SELECT in `sql`. Read-only.",
                _o(path=S, action={"type": "string", "enum": ["tables", "query"]}, sql=S),
                readonly=True, run=_db_query),
        ToolDef("db_execute", "Change a local SQLite database (default .hexiel/data.db): CREATE "
                "TABLE, INSERT, UPDATE, DELETE. For many rows pass a parameterised `sql` "
                "(INSERT … VALUES (?, ?)) plus `rows`.",
                _o(path=S, sql=S, rows={"type": "array", "items": A}), run=_db_execute),
        ToolDef("sheet_read", "Read an Excel workbook: no sheet/range lists sheets and sizes; "
                "otherwise returns the cells of `range` (e.g. A1:F20) on `sheet`.",
                _o(path=S, sheet=S, range=S, formulas={"type": "boolean"}), readonly=True, run=_sheet_read),
        ToolDef("sheet_write", "Write to an Excel workbook, keeping existing formatting: action=write "
                "puts a 2-D `values` array at `start` (e.g. B2); action=append adds rows at the "
                "end; a new `sheet` name creates the sheet. Creates the file if missing.",
                _o(path=S, sheet=S, action={"type": "string", "enum": ["write", "append"]},
                   start=S, values={"type": "array", "items": A}), run=_sheet_write),
        ToolDef("chart", "Make an interactive chart (HTML, opens in any browser) from a SQL query "
                "over data files, or from rows+columns. type: bar | line | scatter | area | pie.",
                _o(title=S, type=S, files={"type": "array", "items": S}, sql=S,
                   rows={"type": "array", "items": A}, columns={"type": "array", "items": S},
                   x=S, y=S, color=S, out=S), run=_chart),
        ToolDef("notebook", "Work with a Jupyter .ipynb notebook: action=cells lists every cell "
                "with its index, type and first lines; replace/insert/delete edit cells by index "
                "(replace writes `source`, insert adds after `index`, -1 = at the end; new cells "
                "default to type `code`, use markdown for prose). Outputs are kept except on the "
                "replaced cell — clear them with clear_output.",
                _o(action={"type": "string", "enum": ["cells", "replace", "insert", "delete",
                                                      "clear_output"]},
                   path=S, index={"type": "integer"}, source=S,
                   kind={"type": "string", "enum": ["code", "markdown", "raw"]}), run=_notebook),
    ]


# ---------------------------------------------------------------- notebook (.ipynb)

_NOTEBOOK_CAP = 120          # cells shown in action=cells before the list is cut


def _nb_source(cell: dict) -> str:
    src = cell.get("source", "")
    return "".join(src) if isinstance(src, list) else str(src)


def _read_notebook(path: Path) -> dict:
    try:
        nb = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read {path.name}: {exc}")
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        raise RuntimeError(f"{path.name} is not a Jupyter notebook (no cells)")
    return nb


def _notebook(args, ctx) -> ToolResult:
    action = args.get("action", "cells")
    path = _resolve_path(str(args.get("path", "")), ctx)
    index = int(args.get("index", 0) or 0)
    if action == "cells":
        try:
            nb = _read_notebook(path)
        except RuntimeError as exc:
            return text_result(f"error: {exc}", True)
        ctx.note_file(str(path))       # read-before-edit tracking, as for the file tools
        rows = []
        for i, cell in enumerate(nb["cells"]):
            first = _nb_source(cell).strip().splitlines()
            head = first[0][:90] if first else "(empty)"
            suffix = f" · output" if cell.get("outputs") else ""
            rows.append(f"[{i}] {cell.get('cell_type', '?')}{suffix}: {head}")
            if i >= _NOTEBOOK_CAP:
                rows.append(f"…[{len(nb['cells']) - _NOTEBOOK_CAP - 1} more cells]")
                break
        return text_result(f"{path.name}: {len(nb['cells'])} cells\n" + "\n".join(rows))
    # everything below rewrites the file: stale-guard + read-before-write as for the file tools
    stale = ctx.stale_refusal(str(path))
    if stale:
        return stale
    try:
        nb = _read_notebook(path)
    except RuntimeError as exc:
        return text_result(f"error: {exc}", True)
    cells = nb["cells"]
    if action in ("replace", "delete", "clear_output") and not 0 <= index < len(cells):
        return text_result(f"error: cell index {index} is out of range (0…{len(cells) - 1})", True)
    source = args.get("source")
    if action in ("replace", "insert"):
        if not isinstance(source, str) or not source.strip():
            return text_result("error: source (the cell's text) is required for replace/insert", True)
    if action == "replace":
        cells[index] = {**cells[index], "cell_type": args.get("kind", cells[index]["cell_type"]),
                        "source": source.splitlines(keepends=True),
                        "outputs": [], "execution_count": None}
        msg = f"replaced cell [{index}] ({cells[index]['cell_type']}); prior outputs cleared"
    elif action == "insert":
        kind = args.get("kind", "code")
        after = index if index >= 0 else len(cells) - 1
        new_cell = {"cell_type": kind, "metadata": {}, "source": source.splitlines(keepends=True)}
        if kind == "code":
            new_cell.update(outputs=[], execution_count=None)
        cells.insert(after + 1, new_cell)
        msg = f"inserted {kind} cell [{after + 1}]"
    elif action == "delete":
        gone = cells.pop(index)
        msg = f"deleted cell [{index}] ({gone.get('cell_type', '?')})"
    elif action == "clear_output":
        cells[index].setdefault("outputs", [])
        del cells[index]["outputs"][:]
        cells[index]["execution_count"] = None
        msg = f"cleared outputs of cell [{index}]"
    else:
        return text_result("error: unknown action", True)
    err = _write_notebook(path, nb)
    if not err:
        ctx.note_file(str(path))       # the write is now what we last saw
    return err or text_result(msg)


def _write_notebook(path: Path, nb: dict) -> ToolResult | None:
    """Persist a mutation; an error tool-result (rare, e.g. disk full) is returned instead of the ok text."""
    try:
        path.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + "\n")
    except (OSError, TypeError) as exc:
        return text_result(f"error: could not save {path.name}: {exc}", True)
    return None

