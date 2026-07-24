# freecad/llm_copilot/chat_panel.py
import html
import threading
from PySide import QtGui, QtCore
import FreeCAD
import FreeCADGui as Gui

from . import document_inspector, script_executor, llm_client
from .agent import Agent
from .settings import load_settings, PARAM_PATH

class _Client:
    def complete(self, messages, settings):
        return llm_client.complete(messages, settings)


class _MainThreadRunner(QtCore.QObject):
    """Runs a callable on the GUI (main) thread and blocks the caller until it
    finishes, returning its result or re-raising its exception.

    FreeCAD/Qt is NOT thread-safe: executing document scripts triggers GUI
    updates (tree view, 3D view, Sketcher solver feedback) that must happen on
    the main thread. The LLM network call stays on a worker thread, but every
    FreeCAD access is funneled through here. Uses a queued signal so the slot
    runs on this object's (main) thread regardless of who calls run().
    """
    _invoke = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._invoke.connect(self._run_slot, QtCore.Qt.QueuedConnection)

    def _run_slot(self, box):
        try:
            box["result"] = box["fn"]()
        except BaseException as exc:  # noqa: BLE001 - re-raised on caller thread
            box["error"] = exc
        finally:
            box["done"].set()

    def run(self, fn):
        # Fast path: already on the GUI thread — call directly (e.g. undo button).
        if QtCore.QThread.currentThread() is self.thread():
            return fn()
        box = {"fn": fn, "done": threading.Event(), "result": None, "error": None}
        self._invoke.emit(box)
        box["done"].wait()
        if box["error"] is not None:
            raise box["error"]
        return box["result"]

class CopilotDockWidget(QtGui.QDockWidget):
    # Append a line to the log (worker -> GUI thread; Qt queues cross-thread).
    resultReady = QtCore.Signal(str)
    # Ask the user to confirm an intent. Carries a threading.Event + a mutable
    # result dict so the worker can block until the GUI answers, without relying
    # on QTimer.singleShot (which isn't reliably serviced from a worker thread).
    intentAsked = QtCore.Signal(str, object, object)
    # Toggle input/button enabled state from the worker thread.
    busyChanged = QtCore.Signal(bool)
    # Model reasoning text captured from the response (worker -> GUI thread).
    reasoningReady = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__("LLM Copilot", parent)
        self.setObjectName("LLMCopilotDock")
        body = QtGui.QWidget()
        layout = QtGui.QVBoxLayout(body)
        # QTextBrowser (not QTextEdit) so the collapsible "Thinking" toggle can
        # be a clickable anchor. We render the whole transcript from _entries.
        self.log = QtGui.QTextBrowser()
        self.log.setOpenLinks(False)
        self.log.anchorClicked.connect(self._on_anchor)
        self.input = QtGui.QLineEdit()
        self.send_btn = QtGui.QPushButton("Send")
        self.undo_btn = QtGui.QPushButton("Undo last change")
        layout.addWidget(self.log)
        layout.addWidget(self.input)
        layout.addWidget(self.send_btn)
        layout.addWidget(self.undo_btn)
        self.setWidget(body)
        self.send_btn.clicked.connect(self._on_send)
        self.input.returnPressed.connect(self._on_send)  # Enter sends
        self.undo_btn.clicked.connect(self._on_undo)
        # These signals are emitted from the worker thread, so force a queued
        # connection: the slots must run on the GUI thread (Qt widgets use
        # timers that must not start on a worker thread).
        self.resultReady.connect(self._append, QtCore.Qt.QueuedConnection)
        self.intentAsked.connect(self._ask_intent, QtCore.Qt.QueuedConnection)
        self.busyChanged.connect(self._set_busy, QtCore.Qt.QueuedConnection)
        self.reasoningReady.connect(self._add_reasoning, QtCore.Qt.QueuedConnection)
        # Transcript model: list of {"kind": "text"|"reasoning", ...}.
        self._entries = []
        self._reason_seq = 0
        self._expanded = set()
        self._busy = False
        # Marshals every FreeCAD access to the main thread (FreeCAD/Qt is not
        # thread-safe; running document scripts off-thread corrupts the GUI).
        self._main = _MainThreadRunner(self)
        # Route client diagnostics to the FreeCAD report view so a "no response"
        # can be traced (e.g. model replied with text instead of a tool call).
        llm_client.DEBUG_LOG = lambda m: FreeCAD.Console.PrintMessage(
            "[LLM Copilot] " + m + "\n")
        self._build_agent()

    def _build_agent(self):
        settings = load_settings(FreeCAD.ParamGet(PARAM_PATH))
        main = self._main

        # Wrap FreeCAD-touching calls so they run on the main thread even though
        # the agent loop runs on a worker thread.
        def inspector(app):
            return main.run(lambda: document_inspector.snapshot(app))

        class _MarshalledExecutor:
            def run(self, app, script):
                return main.run(lambda: script_executor.run(app, script))
            def undo(self, app):
                return main.run(lambda: script_executor.undo(app))

        self.agent = Agent(client=_Client(),
                           inspector=inspector,
                           executor=_MarshalledExecutor(),
                           app=FreeCAD, settings=settings)

    def _append(self, text):
        """Add a plain (HTML) transcript line. Empty strings are ignored."""
        if text == "":
            return
        self._entries.append({"kind": "text", "html": text})
        self._render()

    def _add_reasoning(self, reasoning):
        """Add a collapsible 'Thinking' entry holding the model's reasoning."""
        if not reasoning:
            return
        self._reason_seq += 1
        self._entries.append({
            "kind": "reasoning", "id": self._reason_seq, "text": reasoning,
        })
        self._render()

    def _on_anchor(self, url):
        ref = url.toString()
        if ref.startswith("reasoning:"):
            rid = int(ref.split(":", 1)[1])
            if rid in self._expanded:
                self._expanded.discard(rid)
            else:
                self._expanded.add(rid)
            self._render()

    def _render(self):
        parts = []
        for e in self._entries:
            if e["kind"] == "text":
                parts.append(e["html"])
            else:
                rid = e["id"]
                expanded = rid in self._expanded
                arrow = "&#9662;" if expanded else "&#9656;"  # ▾ / ▸
                parts.append(
                    f'<a href="reasoning:{rid}" style="text-decoration:none;'
                    f'color:gray;"><i>{arrow} Thinking</i></a>')
                if expanded:
                    body = html.escape(e["text"]).replace("\n", "<br>")
                    parts.append(
                        f'<div style="color:gray;margin-left:1em;">{body}</div>')
        self.log.setHtml("<br>".join(parts))
        # keep scrolled to the bottom
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _ask_intent(self, intent, answer, done):
        """GUI-thread slot: show the confirm dialog, store the answer, unblock
        the worker. Invoked via the intentAsked signal (queued from the worker)."""
        try:
            res = QtGui.QMessageBox.question(
                self, "Run this change?", intent,
                QtGui.QMessageBox.Yes | QtGui.QMessageBox.No)
            answer["ok"] = (res == QtGui.QMessageBox.Yes)
        finally:
            done.set()

    def _set_busy(self, busy):
        self._busy = busy
        self.send_btn.setEnabled(not busy)
        self.input.setEnabled(not busy)

    def _on_send(self):
        if self._busy:
            return
        msg = self.input.text().strip()
        if not msg:
            return
        self.input.clear()
        self._append(f"<b>You:</b> {msg}")
        self._append("<i>Thinking…</i>")
        self._build_agent()  # pick up latest settings each send
        self.busyChanged.emit(True)

        def on_intent(intent):
            # Block the worker until the GUI answers, marshaling via a queued
            # signal (reliable from a non-GUI thread, unlike singleShot).
            answer = {}
            done = threading.Event()
            self.intentAsked.emit(intent, answer, done)
            done.wait()
            return answer.get("ok", False)

        def on_result(result, snap):
            status = "OK" if result.ok else f"ERROR: {result.error.splitlines()[-1]}"
            self.resultReady.emit(f"<i>step: {status}</i>")

        def on_reasoning(reasoning):
            self.reasoningReady.emit(reasoning)

        def work():
            try:
                out = self.agent.send(msg, on_intent, on_result, on_reasoning)
                self.resultReady.emit(f"<b>Copilot:</b> {out}")
            except Exception as e:
                import traceback
                FreeCAD.Console.PrintError(
                    "LLM Copilot error:\n" + traceback.format_exc())
                self.resultReady.emit(f"<b>Error:</b> {e}")
            finally:
                self.busyChanged.emit(False)  # re-enable input on the GUI thread

        threading.Thread(target=work, daemon=True).start()

    def _on_undo(self):
        script_executor.undo(FreeCAD)
        self._append("<i>Undid last change.</i>")

_dock = None

def _ensure_dock():
    """Create the dock widget once and attach it to the main window.

    Attaching it makes it appear (and be toggleable) under View -> Panels in
    every workbench, so the copilot is ambient rather than tied to a mode.
    """
    global _dock
    if _dock is None:
        mw = Gui.getMainWindow()
        _dock = CopilotDockWidget(mw)
        mw.addDockWidget(QtCore.Qt.RightDockWidgetArea, _dock)
    return _dock

def create_panel(visible=False):
    """Install the dock at startup. Hidden by default so it doesn't intrude;
    the user reveals it via View -> Panels or the toggle shortcut."""
    dock = _ensure_dock()
    dock.setVisible(visible)
    return dock

def toggle_panel():
    dock = _ensure_dock()
    dock.setVisible(not dock.isVisible())
