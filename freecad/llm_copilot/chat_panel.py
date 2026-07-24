# freecad/llm_copilot/chat_panel.py
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

class CopilotDockWidget(QtGui.QDockWidget):
    resultReady = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__("LLM Copilot", parent)
        self.setObjectName("LLMCopilotDock")
        body = QtGui.QWidget()
        layout = QtGui.QVBoxLayout(body)
        self.log = QtGui.QTextEdit(); self.log.setReadOnly(True)
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
        self.resultReady.connect(self._append)
        self._build_agent()

    def _build_agent(self):
        settings = load_settings(FreeCAD.ParamGet(PARAM_PATH))
        self.agent = Agent(client=_Client(),
                           inspector=document_inspector.snapshot,
                           executor=script_executor,
                           app=FreeCAD, settings=settings)

    def _append(self, text):
        self.log.append(text)

    def _on_send(self):
        msg = self.input.text().strip()
        if not msg:
            return
        self.input.clear()
        self._append(f"<b>You:</b> {msg}")
        self._build_agent()  # pick up latest settings each send

        def on_intent(intent):
            # Ask on the GUI thread; block worker until answered.
            answer = {}
            done = threading.Event()
            def ask():
                res = QtGui.QMessageBox.question(
                    self, "Run this change?", intent,
                    QtGui.QMessageBox.Yes | QtGui.QMessageBox.No)
                answer["ok"] = (res == QtGui.QMessageBox.Yes)
                done.set()
            QtCore.QTimer.singleShot(0, ask)
            done.wait()
            return answer.get("ok", False)

        def on_result(result, snap):
            status = "OK" if result.ok else f"ERROR: {result.error.splitlines()[-1]}"
            self.resultReady.emit(f"<i>step: {status}</i>")

        def work():
            try:
                out = self.agent.send(msg, on_intent, on_result)
            except Exception as e:
                out = f"Error: {e}"
            self.resultReady.emit(f"<b>Copilot:</b> {out}")

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
