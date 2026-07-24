"""Pure-threading test of the marshaller contract used by _MainThreadRunner.

The real _MainThreadRunner (in chat_panel.py) needs PySide + a Qt event loop,
which isn't available outside the FreeCAD GUI. This test reproduces its exact
control-flow contract with a plain worker thread standing in for the "GUI
thread", so the blocking/result/exception logic is regression-tested even though
the Qt wiring itself can only be exercised in the GUI.
"""

import threading
import queue


class _StubRunner:
    """Mirrors _MainThreadRunner.run(): marshal fn to the 'main' thread, block
    until done, return its result or re-raise its exception."""

    def __init__(self):
        self.main_thread = threading.current_thread()
        self._q = queue.Queue()

    def pump_once(self):
        """Stand-in for the Qt event loop processing one queued invocation."""
        box = self._q.get()
        try:
            box["result"] = box["fn"]()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            box["done"].set()

    def run(self, fn):
        if threading.current_thread() is self.main_thread:
            return fn()
        box = {"fn": fn, "done": threading.Event(), "result": None, "error": None}
        self._q.put(box)
        box["done"].wait()
        if box["error"] is not None:
            raise box["error"]
        return box["result"]


def test_marshals_result_from_worker_to_main():
    runner = _StubRunner()
    ran_on = {}

    def fc_call():
        ran_on["thread"] = threading.current_thread()
        return 42

    out = {}
    def worker():
        out["value"] = runner.run(fc_call)

    t = threading.Thread(target=worker)
    t.start()
    runner.pump_once()   # "main thread" services the invocation
    t.join(timeout=5)

    assert out["value"] == 42
    # the FreeCAD-touching call ran on the main thread, not the worker
    assert ran_on["thread"] is runner.main_thread


def test_exception_reraised_on_caller():
    runner = _StubRunner()

    def boom():
        raise ValueError("bad script")

    result = {}
    def worker():
        try:
            runner.run(boom)
        except ValueError as e:
            result["err"] = str(e)

    t = threading.Thread(target=worker)
    t.start()
    runner.pump_once()
    t.join(timeout=5)

    assert result["err"] == "bad script"


def test_fast_path_runs_directly_on_main_thread():
    runner = _StubRunner()
    # Called from the main thread: must run inline without needing a pump.
    assert runner.run(lambda: "inline") == "inline"
