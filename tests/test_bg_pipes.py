"""A command that starts a background process (`nohup server &`) must return when the shell exits, not hang until
the background process closes the pipes it inherited."""
import time

from hexiel.tools import run_cancellable


def test_backgrounded_child_holding_the_pipes_does_not_hang_the_tool():
    t = time.time()
    r = run_cancellable(["bash", "-c", "sleep 20 & echo started"], timeout=60)
    assert "started" in r.stdout
    assert time.time() - t < 10
