import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run(*args, cwd=ROOT):
    r = subprocess.run([sys.executable, str(ROOT / "stats.py"), *args], capture_output=True, text=True, cwd=cwd)
    return r.returncode, r.stdout, r.stderr


def csv_file(tmp_path, text):
    p = tmp_path / "d.csv"
    p.write_text(text)
    return str(p)


def test_readme_example():
    code, out, _ = run("sales.csv", "--group-by", "region", "--agg", "sum:amount", "avg:amount")
    assert code == 0
    assert out.splitlines() == ["region,sum(amount),avg(amount)", "east,30,15.00", "north,17.50,8.75", "west,5,5.00"]


def test_no_group_default_count():
    assert run("sales.csv")[1].splitlines() == ["count(*)", "5"]


def test_no_group_multiple_aggs_order_preserved():
    out = run("sales.csv", "--agg", "max:amount", "min:amount", "count:units", "sum:units")[1].splitlines()
    assert out == ["max(amount),min(amount),count(units),sum(units)", "20,5,4,11"]


def test_empty_cells_ignored_and_counted_right():
    out = run("sales.csv", "--group-by", "region", "--agg", "count:units", "count:*", "avg:units")[1].splitlines()
    assert out == ["region,count(units),count(*),avg(units)", "east,2,2,1.50", "north,1,2,3.00", "west,1,1,5.00"]


def test_all_empty_group(tmp_path):
    f = csv_file(tmp_path, "g,v\na,\na,\nb,4\n")
    out = run(f, "--group-by", "g", "--agg", "sum:v", "avg:v", "min:v", "max:v", "count:v")[1].splitlines()
    assert out == ["g,sum(v),avg(v),min(v),max(v),count(v)", "a,0,,,,0", "b,4,4.00,4,4,1"]


def test_whole_vs_decimal_formatting(tmp_path):
    f = csv_file(tmp_path, "v\n1.5\n2.5\n3\n")
    out = run(f, "--agg", "sum:v", "min:v", "max:v", "avg:v")[1].splitlines()
    assert out == ["sum(v),min(v),max(v),avg(v)", "7,1.50,3,2.33"]


def test_none_group_sorts_last_and_is_labelled(tmp_path):
    f = csv_file(tmp_path, "g,v\nb,1\n,2\na,3\n")
    out = run(f, "--group-by", "g", "--agg", "sum:v")[1].splitlines()
    assert out == ["g,sum(v)", "a,3", "b,1", "(none),2"]


def test_default_group_order_is_plain_string_comparison(tmp_path):
    f = csv_file(tmp_path, "g,v\n10,1\n9,1\n2,1\n")
    assert run(f, "--group-by", "g")[1].splitlines() == ["g,count(*)", "10,1", "2,1", "9,1"]


def test_sort_descending_numeric():
    out = run("sales.csv", "--group-by", "region", "--agg", "sum:amount", "--sort", "-sum(amount)")[1].splitlines()
    assert out == ["region,sum(amount)", "east,30", "north,17.50", "west,5"]


def test_sort_ascending_numeric_not_string():
    out = run("sales.csv", "--group-by", "product", "--agg", "sum:amount", "--sort", "sum(amount)")[1].splitlines()
    assert out == ["product,sum(amount)", "pen,22.50", "book,30"]


def test_sort_equals_form_also_works():
    out = run("sales.csv", "--group-by", "region", "--agg", "sum:amount", "--sort=-sum(amount)")[1].splitlines()
    assert out == ["region,sum(amount)", "east,30", "north,17.50", "west,5"]


def test_sort_ties_broken_by_group_ascending(tmp_path):
    f = csv_file(tmp_path, "g,v\nb,1\na,1\nc,2\n")
    assert run(f, "--group-by", "g", "--agg", "sum:v", "--sort", "-sum(v)")[1].splitlines() == ["g,sum(v)", "c,2", "a,1", "b,1"]


def test_sort_empty_cells_last(tmp_path):
    f = csv_file(tmp_path, "g,v\na,\nb,2\nc,1\n")
    assert run(f, "--group-by", "g", "--agg", "min:v", "--sort", "min(v)")[1].splitlines() == ["g,min(v)", "c,1", "b,2", "a,"]
    assert run(f, "--group-by", "g", "--agg", "min:v", "--sort", "-min(v)")[1].splitlines() == ["g,min(v)", "b,2", "c,1", "a,"]


def test_sort_by_group_column_descending():
    out = run("sales.csv", "--group-by", "region", "--sort", "-region")[1].splitlines()
    assert out == ["region,count(*)", "west,1", "north,2", "east,2"]


def test_error_cannot_read():
    code, out, err = run("nope.csv")
    assert (code, out, err.strip()) == (1, "", "error: cannot read nope.csv")


def test_error_unknown_columns():
    for args, name in ((["--group-by", "zzz"], "zzz"), (["--agg", "sum:zzz"], "zzz"), (["--sort", "-bogus"], "bogus")):
        code, out, err = run("sales.csv", *args)
        assert (code, out, err.strip()) == (2, "", f"error: unknown column '{name}'")


def test_error_unknown_function():
    code, out, err = run("sales.csv", "--agg", "median:amount")
    assert (code, out, err.strip()) == (2, "", "error: unknown function 'median'")


def test_error_non_number_reports_row_and_column(tmp_path):
    f = csv_file(tmp_path, "g,v\na,1\na,2\nb,abc\nb,4\n")
    code, out, err = run(f, "--group-by", "g", "--agg", "sum:v")
    assert (code, out, err.strip()) == (3, "", "error: row 3: 'abc' is not a number (column 'v')")


def test_check_order_columns_before_values(tmp_path):
    f = csv_file(tmp_path, "g,v\na,abc\n")
    code, _, err = run(f, "--group-by", "nope", "--agg", "sum:v")
    assert code == 2 and "unknown column 'nope'" in err


def test_count_on_text_column_is_fine():
    out = run("sales.csv", "--group-by", "region", "--agg", "count:product")[1].splitlines()
    assert out == ["region,count(product)", "east,2", "north,2", "west,1"]
