"""Data tools: schema cards, SQL over files, Excel, export, SQLite, charts, lazy loading."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openpyxl

from hexiel import datatools as D
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext


def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), chain=Toolchain())


def make_files(tmp_path):
    (tmp_path / "sales.csv").write_text("region,month,amount\n" + "".join(
        f"{r},2026-0{m},{100 * m + i}\n" for i, r in enumerate(["north", "south", "east"]) for m in range(1, 7)))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Budget"
    ws.append(["region", "target"])
    for r, t in (("north", 2000), ("south", 1800), ("east", 2500)):
        ws.append([r, t])
    ws["B2"].font = openpyxl.styles.Font(bold=True)
    wb.save(tmp_path / "plan.xlsx")


def test_schema_card_is_compact(tmp_path):
    make_files(tmp_path)
    out = D._data({"action": "schema", "files": ["sales.csv"]}, ctx(tmp_path)).text
    assert "table sales: 18 rows, 3 columns" in out and "amount" in out and "sample rows" in out
    assert len(out) < 1500                       # a card, not the file


def test_sql_across_csv_and_excel(tmp_path):
    make_files(tmp_path)
    r = D._data({"files": ["sales.csv", "plan.xlsx"], "sql":
                 "SELECT s.region, sum(amount) AS total, p.target FROM sales s JOIN plan p "
                 "ON s.region = p.region GROUP BY ALL ORDER BY total DESC"}, ctx(tmp_path))
    assert not r.is_error, r.text
    lines = r.text.splitlines()
    assert lines[0] == "region | total | target" and lines[2].startswith("east | 2112")


def test_errors_name_the_tables(tmp_path):
    make_files(tmp_path)
    r = D._data({"files": ["sales.csv"], "sql": "SELECT * FROM nope"}, ctx(tmp_path))
    assert r.is_error and "tables: sales" in r.text


def test_export_xlsx_and_csv(tmp_path):
    make_files(tmp_path)
    sql = "SELECT region, sum(amount) total FROM sales GROUP BY 1 ORDER BY 1"
    assert "saved 3 row(s)" in D._data_export({"files": ["sales.csv"], "sql": sql, "out": "out/t.xlsx"}, ctx(tmp_path)).text
    ws = openpyxl.load_workbook(tmp_path / "out/t.xlsx").active
    assert [c.value for c in ws[1]] == ["region", "total"] and ws.max_row == 4
    D._data_export({"files": ["sales.csv"], "sql": sql, "out": "t.csv"}, ctx(tmp_path))
    assert (tmp_path / "t.csv").read_text().startswith("region,total")


def test_sqlite_create_insert_query(tmp_path):
    c = ctx(tmp_path)
    assert not D._db_execute({"sql": "CREATE TABLE contacts (name TEXT, email TEXT);"}, c).is_error
    D._db_execute({"sql": "INSERT INTO contacts VALUES (?, ?)", "rows": [["Ann", "a@x"], ["Bo", "b@x"]]}, c)
    assert "2 row(s)" in D._db_query({"action": "tables"}, c).text
    q = D._db_query({"sql": "SELECT name FROM contacts ORDER BY name"}, c)
    assert "Ann" in q.text and "Bo" in q.text
    blocked = D._db_query({"sql": "DELETE FROM contacts"}, c)
    assert blocked.is_error and "read-only" in blocked.text


def test_sheet_write_keeps_formatting(tmp_path):
    make_files(tmp_path)
    c = ctx(tmp_path)
    D._sheet_write({"path": "plan.xlsx", "sheet": "Budget", "start": "C1", "values": [["actual"], [2112]]}, c)
    D._sheet_write({"path": "plan.xlsx", "sheet": "Budget", "action": "append", "values": [["west", 900]]}, c)
    wb = openpyxl.load_workbook(tmp_path / "plan.xlsx")
    ws = wb["Budget"]
    assert ws["C1"].value == "actual" and ws["C2"].value == 2112 and ws["A5"].value == "west"
    assert ws["B2"].font.bold                       # existing formatting kept
    assert "Budget: 5 rows" in D._sheet_read({"path": "plan.xlsx"}, c).text


def test_chart_html(tmp_path):
    make_files(tmp_path)
    r = D._chart({"title": "Sales by region", "files": ["sales.csv"],
                  "sql": "SELECT region, sum(amount) total FROM sales GROUP BY 1", "type": "bar"}, ctx(tmp_path))
    html = (tmp_path / "charts" / "sales_by_region.html").read_text()
    assert "vega-lite" in html and '"total"' in html and "3 point(s)" in r.text


def test_data_tools_load_lazily(tmp_path):
    from hexiel import toolsets
    from hexiel.config import ModelConfig, Settings
    from hexiel.session import Session
    s = Session(ModelConfig(name="t", provider="openai", model="m", base_url="http://x"),
                Settings(), str(tmp_path), [], "sys")
    toolsets.install(s, Settings())
    assert [t.name for t in s.tools] == ["load_tools"]          # code project: nothing extra
    loader = s.tools[0]
    assert "sheet_write" in loader.run({"group": "data"}, None).text
    assert "data" in {t.name for t in s.tools}
    (tmp_path / "x.csv").write_text("a\n1\n")
    s2 = Session(s.model_config, Settings(), str(tmp_path), [], "sys")
    toolsets.install(s2, Settings())
    assert "data" in {t.name for t in s2.tools}                 # data project: auto-loaded
