import threading

from hexiel import main as main_mod


def test_returns_normally_when_no_worker_is_busy(monkeypatch):
    monkeypatch.setattr("os._exit", lambda code: (_ for _ in ()).throw(AssertionError("must not force-exit")))
    main_mod.leave_without_waiting()


def test_force_exits_instead_of_waiting_for_a_busy_non_daemon_thread(monkeypatch):
    release = threading.Event()
    worker = threading.Thread(target=release.wait, name="hexiel-tools_0")      # like a tool-pool worker mid-command
    worker.start()
    calls = []
    monkeypatch.setattr("os._exit", lambda code: calls.append(code))
    monkeypatch.setattr("atexit._run_exitfuncs", lambda: calls.append("cleanups"))
    try:
        main_mod.leave_without_waiting()
    finally:
        release.set()
        worker.join()
    assert calls == ["cleanups", 0]
