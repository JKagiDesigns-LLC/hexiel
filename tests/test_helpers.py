"""The util tool's deterministic helpers."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import helpers as h


def test_calc_is_safe_and_exact():
    assert h.run("calc", {"expr": "(17*3)**2 / 3"}, ".") == "(17*3)**2 / 3 = 867"
    assert h.calc("sqrt(16) + 2^3") == 12
    for bad in ("__import__('os')", "open('x')", "2**999999"):
        with pytest.raises(h.HelperError):
            h.calc(bad)


def test_dates():
    assert h.date("2026-11-11 + 45 days").startswith("2026-12-26")
    assert h.date("2026-01-31 + 1 month").startswith("2026-02-28")
    assert h.date("2024-02-29 + 1 year").startswith("2025-02-28")
    assert h.date("2026-01-01 to 2026-11-11").startswith("314 days")
    assert "Sunday" in h.date("weekday 2027-02-14")
    assert h.date("2026-11-11 + 2 weeks - 1 day").startswith("2026-11-24")


def test_units():
    assert h.units("5 mi to km") == "5 mi = 8.04672 km"
    assert h.units("212 F to C") == "212 F = 100 C"
    assert h.units("1 GiB to MB") == "1 GiB = 1073.741824 MB"
    with pytest.raises(h.HelperError):
        h.units("5 kg to km")


def test_regex_json_hash_base64():
    assert "groups=['2026', '11']" in h.regex(r"(\d{4})-(\d\d)", "on 2026-11-11")
    assert h.regex("x", "abc") == "no match"
    doc = '{"items": [{"name": "a"}, {"name": "b"}], "n": {"deep": [1, 2, 3]}}'
    assert h.json_path("items[*].name", doc) == '[\n  "a",\n  "b"\n]'
    assert h.json_path("n.deep[-1]", doc) == "3"
    assert h.json_path("missing.x", doc) == "no value at that path"
    assert h.run("hash", {"text": "abc"}, ".").endswith("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")  # pragma: allowlist secret
    assert h.run("base64", {"text": "hello"}, ".") == "aGVsbG8="
    assert h.run("base64", {"text": "aGVsbG8", "mode": "decode"}, ".") == "hello"


def test_semver():
    assert h.semver("1.2.3 < 1.10.0").startswith("true")
    assert h.semver("sort 1.10.0 1.2.0 1.2.0-rc1") == "1.2.0-rc1 < 1.2.0 < 1.10.0"
    assert h.semver("bump minor 1.4.2") == "1.5.0"
    assert h.semver("1.5.0 satisfies ^1.2") == "yes"
    assert h.semver("2.0.0 satisfies ^1.2") == "no"
    assert h.semver("0.2.5 satisfies ~0.2.1") == "yes"


def test_csv_stats(tmp_path):
    f = tmp_path / "sales.csv"
    f.write_text("region,amount,note\nnorth,10,a\nsouth,30,\nnorth,20,b\n")
    out = h.run("csv", {"path": str(f)}, str(tmp_path))
    assert "3 rows × 3 columns" in out
    assert "amount: number, min 10, max 30, mean 20" in out
    assert "region: text, 2 distinct, 0 empty, most common 'north'" in out
    assert "note: text, 2 distinct, 1 empty" in out
