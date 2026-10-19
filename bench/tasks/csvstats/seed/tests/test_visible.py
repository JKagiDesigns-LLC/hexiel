import subprocess
import sys


def run(*args):
    return subprocess.run([sys.executable, "stats.py", *args], capture_output=True, text=True)


def test_count_default():
    r = run("sales.csv")
    assert r.stdout.splitlines() == ["count(*)", "5"]
