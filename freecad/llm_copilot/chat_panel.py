# freecad/llm_copilot/chat_panel.py
import base64
import html
import os
import threading
from PySide import QtGui, QtCore
import FreeCAD
import FreeCADGui as Gui

from . import (
    document_inspector, script_executor, llm_client, view_capture, history_store,
)
from .agent import Agent, AgentCancelled
from .context_usage import format_usage
from .document_binding import PinnedDocumentApp, run_with_document
from .image_processing import reference_triptych
from .markdown import to_html as markdown_to_html
from .settings import load_settings, model_display_name, PARAM_PATH
from .transcript_format import (
    wrappable_escape as _wrappable_escape,
    wrapped_pre as _wrapped_pre,
)

class _Client:
    def complete(self, messages, settings):
        return llm_client.complete(messages, settings)

    def system_prompt(self, settings):
        return llm_client.system_prompt(settings)


class _ResponsiveImageLabel(QtGui.QLabel):
    """Aspect-preserving image that can shrink with the transcript viewport."""
    def __init__(self, image, parent=None):
        super().__init__(parent)
        self._source = QtGui.QPixmap.fromImage(image)
        self.setMinimumWidth(0)
        self.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        self.setSizePolicy(
            QtGui.QSizePolicy.Ignored, QtGui.QSizePolicy.Preferred)
        self._update_pixmap(min(self._source.width(), 320))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        if self._source.isNull() or self._source.width() <= 0:
            return 0
        target = min(max(1, width), self._source.width())
        return max(1, round(
            self._source.height() * target / self._source.width()))

    def sizeHint(self):
        width = min(self._source.width(), 320)
        return QtCore.QSize(width, self.heightForWidth(width))

    def resizeEvent(self, event):
        self._update_pixmap(self.width())
        super().resizeEvent(event)

    def _update_pixmap(self, width):
        if self._source.isNull():
            return
        target = min(max(1, width), self._source.width())
        self.setPixmap(self._source.scaledToWidth(
            target, QtCore.Qt.SmoothTransformation))


class _DisclosureHeader(QtGui.QWidget):
    """Clickable disclosure header whose title can wrap to multiple lines."""
    toggled = QtCore.Signal(bool)

    def __init__(self, title, expanded=False, parent=None):
        super().__init__(parent)
        self._opened = bool(expanded)
        layout = QtGui.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.arrow = QtGui.QToolButton()
        self.arrow.setAutoRaise(True)
        self.arrow.setArrowType(
            QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)
        self.arrow.clicked.connect(self.toggle)
        self.title = QtGui.QLabel(_wrappable_escape(title))
        self.title.setTextFormat(QtCore.Qt.RichText)
        self.title.setWordWrap(True)
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(
            QtGui.QSizePolicy.Ignored, QtGui.QSizePolicy.Preferred)
        self.title.setCursor(QtCore.Qt.PointingHandCursor)
        self.title.installEventFilter(self)
        layout.addWidget(self.arrow, 0, QtCore.Qt.AlignTop)
        layout.addWidget(self.title, 1)

    def eventFilter(self, watched, event):
        if (watched is self.title
                and event.type() == QtCore.QEvent.MouseButtonRelease
                and event.button() == QtCore.Qt.LeftButton):
            self.toggle()
            return True
        return super().eventFilter(watched, event)

    def toggle(self):
        self._opened = not self._opened
        self.arrow.setArrowType(
            QtCore.Qt.DownArrow if self._opened else QtCore.Qt.RightArrow)
        self.toggled.emit(self._opened)


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
    resultReady = QtCore.Signal(object, str)
    # Ask the user to confirm an intent. Carries a threading.Event + a mutable
    # result dict so the worker can block until the GUI answers, without relying
    # on QTimer.singleShot (which isn't reliably serviced from a worker thread).
    intentAsked = QtCore.Signal(object, str, object, object)
    # Toggle input/button enabled state from the worker thread.
    busyChanged = QtCore.Signal(object, bool)
    # Model reasoning text captured from the response (worker -> GUI thread).
    reasoningReady = QtCore.Signal(object, str)
    # Completed script step: document key, intent, Python, ExecResult.
    stepReady = QtCore.Signal(object, str, str, object)
    # Tool call proposed by the model, before execution/dispatch.
    toolReady = QtCore.Signal(object, str, str, str)
    # Newly-added request context about to be sent to the model.
    contextReady = QtCore.Signal(object, object)
    # Structured clarification question: document key, proposal, answer box,
    # completion event.
    questionAsked = QtCore.Signal(object, object, object, object)
    timeoutAsked = QtCore.Signal(object, str, object, object)

    def __init__(self, parent=None):
        super().__init__("LLM Copilot", parent)
        self.setObjectName("LLMCopilotDock")
        body = QtGui.QWidget()
        layout = QtGui.QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        # Persistent entry widgets: appending or updating one message never
        # rebuilds the rest of the transcript.
        self.log = QtGui.QScrollArea()
        self.log.setObjectName("CopilotTranscript")
        self.log.setWidgetResizable(True)
        self.log.setFrameShape(QtGui.QFrame.NoFrame)
        self.log.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.log.setStyleSheet(
            "QScrollArea#CopilotTranscript {"
            " background: palette(base);"
            " border: 1px solid palette(mid);"
            " margin: 8px;"
            "}")
        self.input = QtGui.QLineEdit()
        self.send_btn = QtGui.QPushButton("Send")
        self.cancel_btn = QtGui.QPushButton("Cancel")
        self.cancel_btn.setVisible(False)
        self.attach_btn = QtGui.QPushButton("Attach images…")
        self.undo_btn = QtGui.QPushButton("Undo last change")
        self._pending_images = []
        self.attachment_bar = QtGui.QWidget()
        self.attachment_layout = QtGui.QVBoxLayout(self.attachment_bar)
        self.attachment_layout.setContentsMargins(0, 0, 0, 0)
        self.attachment_layout.addStretch(1)
        self.attachment_bar.setVisible(False)
        context_row = QtGui.QHBoxLayout()
        self.context_label = QtGui.QLabel()
        self.context_label.setToolTip(
            "Estimated from message text, system instructions, and tools; "
            "the provider's exact token count may differ.")
        self.clear_btn = QtGui.QPushButton("Clear context")
        context_row.addWidget(self.context_label, 1)
        context_row.addWidget(self.clear_btn)
        layout.addWidget(self.log)

        footer = QtGui.QWidget()
        footer.setObjectName("CopilotComposer")
        footer.setStyleSheet(
            "QWidget#CopilotComposer {"
            " background: palette(alternate-base);"
            " border-top: 1px solid palette(mid);"
            "}")
        footer_layout = QtGui.QVBoxLayout(footer)
        footer_layout.setContentsMargins(10, 8, 10, 10)
        footer_layout.setSpacing(6)
        footer_layout.addLayout(context_row)
        footer_layout.addWidget(self.attachment_bar)
        footer_layout.addWidget(self.input)
        action_row = QtGui.QHBoxLayout()
        action_row.addWidget(self.send_btn)
        action_row.addWidget(self.cancel_btn)
        footer_layout.addLayout(action_row)
        footer_layout.addWidget(self.attach_btn)
        footer_layout.addWidget(self.undo_btn)
        layout.addWidget(footer)
        self.setWidget(body)
        self.send_btn.clicked.connect(self._on_send)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.input.returnPressed.connect(self._on_send)  # Enter sends
        self.undo_btn.clicked.connect(self._on_undo)
        self.clear_btn.clicked.connect(self._on_clear)
        self.attach_btn.clicked.connect(self._on_attach_images)
        # These signals are emitted from the worker thread, so force a queued
        # connection: the slots must run on the GUI thread (Qt widgets use
        # timers that must not start on a worker thread).
        self.resultReady.connect(self._append, QtCore.Qt.QueuedConnection)
        self.intentAsked.connect(self._ask_intent, QtCore.Qt.QueuedConnection)
        self.busyChanged.connect(self._set_busy, QtCore.Qt.QueuedConnection)
        self.reasoningReady.connect(self._add_reasoning, QtCore.Qt.QueuedConnection)
        self.stepReady.connect(self._add_step, QtCore.Qt.QueuedConnection)
        self.toolReady.connect(self._add_tool, QtCore.Qt.QueuedConnection)
        self.contextReady.connect(self._add_context, QtCore.Qt.QueuedConnection)
        self.questionAsked.connect(
            self._ask_question, QtCore.Qt.QueuedConnection)
        self.timeoutAsked.connect(
            self._ask_timeout, QtCore.Qt.QueuedConnection)
        # Transcript model: list of {"kind": "text"|"reasoning"|"status", ...}.
        # Each FreeCAD document owns an independent agent and transcript.
        self._document_states = {}
        self._document_key = None
        self._entries = []
        self._reason_seq = 0
        self.input.setStyleSheet(
            "QLineEdit:disabled {"
            " background-color: palette(midlight);"
            " color: palette(mid);"
            " border: 2px solid palette(mid);"
            "}")
        # Live "Working… (Ns)" indicator so a slow model call doesn't look frozen.
        self._status_timer = QtCore.QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._tick_status)
        # Marshals every FreeCAD access to the main thread (FreeCAD/Qt is not
        # thread-safe; running document scripts off-thread corrupts the GUI).
        self._main = _MainThreadRunner(self)
        # Route client diagnostics to the FreeCAD report view so a "no response"
        # can be traced (e.g. model replied with text instead of a tool call).
        llm_client.DEBUG_LOG = lambda m: FreeCAD.Console.PrintMessage(
            "[LLM Copilot] " + m + "\n")
        self._document_timer = QtCore.QTimer(self)
        self._document_timer.setInterval(250)
        self._document_timer.timeout.connect(self._sync_document)
        self._sync_document()
        self._document_timer.start()

    def _new_agent(self, settings, document):
        main = self._main

        app = PinnedDocumentApp(FreeCAD, document)

        def on_document(fn):
            """Temporarily activate the owning document for GUI-bound helpers."""
            return run_with_document(FreeCAD, document, fn)

        # Wrap FreeCAD-touching calls so they run on the main thread even though
        # the agent loop runs on a worker thread.
        def inspector(app, rich=False):
            return main.run(
                lambda: on_document(
                    lambda: document_inspector.snapshot(app, rich=rich)))
        inspector.state = lambda app, rich: main.run(
            lambda: on_document(
                lambda: document_inspector.document_state(app, rich=rich)))
        inspector.inspect = lambda app, query: main.run(
            lambda: on_document(lambda: document_inspector.inspect(app, query)))
        from . import api_reference
        inspector.api_lookup = lambda app, query, module, symbol: main.run(
            lambda: api_reference.lookup(app, query, module, symbol))

        class _MarshalledExecutor:
            def run(self, app, script, **kwargs):
                return main.run(
                    lambda: on_document(
                        lambda: script_executor.run(app, script, **kwargs)))
            def undo(self, app):
                return main.run(
                    lambda: on_document(lambda: script_executor.undo(app)))

        def capture_views(changed_names, strategy, max_isolated, **options):
            return main.run(lambda: on_document(lambda: view_capture.capture(
                FreeCAD, changed_names, strategy, max_isolated, **options)))

        return Agent(client=_Client(), inspector=inspector,
                     executor=_MarshalledExecutor(), app=app,
                     settings=settings, view_capture=capture_views)

    def _active_document_key(self):
        doc = getattr(FreeCAD, "ActiveDocument", None)
        return ("document", id(doc)) if doc is not None else ("no-document",)

    def _sync_document(self):
        """Select (or create) state for the active FreeCAD document."""
        key = self._active_document_key()
        if key == self._document_key:
            return
        if self._document_key is not None:
            state = self._document_states[self._document_key]
            state["reason_seq"] = self._reason_seq
            state["scroll"] = self.log.verticalScrollBar().value()
            self.log.takeWidget()
        state = self._document_states.get(key)
        if state is None:
            settings = load_settings(FreeCAD.ParamGet(PARAM_PATH))
            doc = getattr(FreeCAD, "ActiveDocument", None)
            agent = self._new_agent(settings, doc)
            persistence_enabled = settings.persist_chat_history
            messages, entries = (
                history_store.load(doc)
                if doc is not None and persistence_enabled else ([], []))
            if doc is None:
                entries = [{
                    "kind": "text",
                    "html": (
                        "<b>No active document</b>"
                        "<p>Create or open a FreeCAD document to use the "
                        "LLM Copilot.</p>"),
                }]
            agent.messages = messages
            state = {
                "agent": agent, "entries": entries,
                "reason_seq": 0, "step_seq": 0,
                "context_seq": 0,
                "document": doc,
                "scroll": 0,
                "persistence_loaded": persistence_enabled,
                "busy": False, "cancel_event": None,
                "status_entry": None, "elapsed": 0,
                "waiting_for_question": False,
            }
            state["page"], state["layout"] = self._new_transcript_page()
            self._document_states[key] = state
            self._restore_entry_widgets(state)
        self._document_key = key
        self.agent = state["agent"]
        self._entries = state["entries"]
        self._reason_seq = state["reason_seq"]
        self.log.setWidget(state["page"])
        QtCore.QTimer.singleShot(
            0, lambda value=state["scroll"]:
            self.log.verticalScrollBar().setValue(value))
        self._update_context_usage()
        self._update_controls()

    def _update_controls(self):
        available = getattr(FreeCAD, "ActiveDocument", None) is not None
        state = self._document_states.get(self._document_key, {})
        busy = bool(state.get("busy"))
        cancel_event = state.get("cancel_event")
        enabled = available and not busy
        self.send_btn.setEnabled(enabled)
        self.input.setEnabled(enabled)
        self.attach_btn.setEnabled(enabled)
        self.clear_btn.setEnabled(enabled)
        self.undo_btn.setEnabled(enabled)
        self.cancel_btn.setVisible(busy)
        self.cancel_btn.setEnabled(
            busy and cancel_event is not None and not cancel_event.is_set())
        self.input.setPlaceholderText(
            ("Working — press Cancel to stop"
             if busy else
             ("" if available else "Open or create a document first")))

    def _restore_entry_widgets(self, state):
        for entry in state["entries"]:
            kind = entry.get("kind")
            sequence_key = {
                "reasoning": "reason_seq",
                "step": "step_seq",
                "context": "context_seq",
            }.get(kind)
            if sequence_key:
                state[sequence_key] = max(
                    state[sequence_key], int(entry.get("id", 0)))
            self._add_entry_widget(state, entry)

    def _persist_state(self, state=None):
        state = state or self._document_states.get(self._document_key)
        if state is None or state.get("document") is None:
            return
        if not state["agent"].settings.persist_chat_history:
            return
        try:
            history_store.save(
                state["document"], state["agent"].messages, state["entries"])
        except Exception:
            import traceback
            FreeCAD.Console.PrintError(
                "Could not persist LLM Copilot history:\n"
                + traceback.format_exc())

    def _new_transcript_page(self):
        page = QtGui.QWidget()
        page.setObjectName("CopilotTranscriptPage")
        page.setStyleSheet(
            "QWidget#CopilotTranscriptPage { background: palette(base); }")
        layout = QtGui.QVBoxLayout(page)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        layout.addStretch(1)
        return page, layout

    def _is_at_bottom(self):
        bar = self.log.verticalScrollBar()
        return bar.maximum() - bar.value() <= 8

    def _scroll_to_bottom(self):
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _replace_entry_widget(self, state, entry):
        """Rebuild one entry's widget in place, preserving its position.

        Used whenever an entry's rendering changes after it was first shown
        (a question is answered, a timeout is decided, controls are disabled at
        end of turn). Consolidates the find-index / insert / remove / delete
        ritual that was previously copied across several handlers.
        """
        old_widget = entry.get("widget")
        if old_widget is None:
            return None
        index = state["layout"].indexOf(old_widget)
        replacement = self._create_entry_widget(entry)
        state["layout"].insertWidget(index, replacement)
        state["layout"].removeWidget(old_widget)
        old_widget.deleteLater()
        entry["widget"] = replacement
        return replacement

    def _add_entry_widget(self, state, entry):
        follow = (state is self._document_states.get(self._document_key)
                  and self._is_at_bottom())
        widget = self._create_entry_widget(entry)
        insert_at = state["layout"].count() - 1
        # Keep the live Working banner at the end of the transcript while
        # context, reasoning, and script results arrive during the turn.
        status_entry = state.get("status_entry")
        status_widget = (
            status_entry.get("widget") if status_entry is not None else None)
        if status_widget is not None and widget is not status_widget:
            status_index = state["layout"].indexOf(status_widget)
            if status_index >= 0:
                insert_at = status_index
        state["layout"].insertWidget(insert_at, widget)
        entry["widget"] = widget
        if follow:
            QtCore.QTimer.singleShot(0, self._scroll_to_bottom)
        return widget

    def _rich_label(self, markup):
        label = QtGui.QLabel(markup)
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setSizePolicy(
            QtGui.QSizePolicy.Ignored, QtGui.QSizePolicy.Preferred)
        label.setTextFormat(QtCore.Qt.RichText)
        label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse | QtCore.Qt.LinksAccessibleByMouse)
        label.setOpenExternalLinks(True)
        label.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        return label

    def _disclosure(self, title, build_content, expanded=False):
        box = QtGui.QWidget()
        layout = QtGui.QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        header = _DisclosureHeader(title, expanded)
        layout.addWidget(header)
        holder = {"widget": None}

        def changed(opened):
            if holder["widget"] is None:
                holder["widget"] = build_content()
                layout.addWidget(holder["widget"])
            holder["widget"].setVisible(opened)

        header.toggled.connect(changed)
        if expanded:
            changed(True)
        return box

    def _update_context_usage(self):
        messages = self.agent.messages if hasattr(self, "agent") else []
        self.context_label.setText(
            format_usage(messages, llm_client.SYSTEM_PROMPT,
                         llm_client.TOOL_SCHEMA))

    def _create_entry_widget(self, entry):
        kind = entry["kind"]
        if kind == "text":
            return self._rich_label(entry["html"])
        if kind == "user":
            box = QtGui.QWidget()
            layout = QtGui.QVBoxLayout(box)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self._rich_label(
                "<b>You:</b><p>" + _wrappable_escape(entry["text"]) + "</p>"))
            for image in entry.get("images", []):
                layout.addWidget(self._rich_label(
                    "<i>Attached: " + _wrappable_escape(image["name"]) + "</i>"))
                layout.addWidget(self._image_widget(image["data"]))
            return box
        if kind == "status":
            label = self._rich_label(
                f'<b>●&nbsp; {html.escape(entry["text"])}</b>')
            label.setStyleSheet(
                "QLabel {"
                " background-color: palette(highlight);"
                " color: palette(highlighted-text);"
                " border-radius: 4px;"
                " padding: 8px 10px;"
                "}")
            entry["label"] = label
            return label
        if kind == "reasoning":
            return self._disclosure(
                "Thinking",
                lambda: self._rich_label(
                    '<div style="color:gray;">'
                    + _wrappable_escape(entry["text"]).replace("\n", "<br>")
                    + "</div>"))
        if kind == "tool":
            title = "Tool · " + entry["tool"]
            if entry.get("summary"):
                title += " — " + entry["summary"]
            return self._disclosure(
                title,
                lambda: self._rich_label(
                    _wrapped_pre(entry.get("details", ""))))
        if kind == "step":
            result = entry["result"]
            def step_content():
                if result.ok:
                    result_text = "Success"
                    if result.output.strip():
                        result_text += _wrapped_pre(result.output.rstrip())
                else:
                    result_text = "Failed"
                    if result.error:
                        result_text += _wrapped_pre(result.error.rstrip())
                    if result.output.strip():
                        result_text += (
                            "<b>Output</b>"
                            + _wrapped_pre(result.output.rstrip()))
                validation = getattr(result, "validation", "")
                if validation:
                    result_text += (
                        "<b>Validation</b>"
                        + _wrapped_pre(validation.rstrip()))
                stderr = getattr(result, "stderr", "")
                if stderr:
                    result_text += (
                        "<b>Standard error</b>"
                        + _wrapped_pre(stderr.rstrip()))
                warnings = getattr(result, "console_warnings", "")
                if warnings:
                    result_text += (
                        "<b>FreeCAD console warnings</b>"
                        + _wrapped_pre(warnings.rstrip()))
                console_errors = getattr(result, "console_errors", "")
                if console_errors:
                    result_text += (
                        "<b>FreeCAD console errors</b>"
                        + _wrapped_pre(console_errors.rstrip()))
                if getattr(result, "rolled_back", False):
                    result_text += "<b>Rolled back to the previous state.</b>"
                return self._rich_label(
                    '<b>Python</b>' + _wrapped_pre(entry["script"])
                    + "<b>Result</b><br>" + result_text)

            return self._disclosure(
                entry["intent"], step_content)
        if kind == "question":
            return self._question_widget(entry)
        if kind == "timeout":
            return self._timeout_widget(entry)
        return self._context_widget(entry)

    def _timeout_widget(self, entry):
        box = QtGui.QFrame()
        box.setFrameShape(QtGui.QFrame.StyledPanel)
        layout = QtGui.QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.addWidget(self._rich_label(
            "<b>Model request timed out</b><p>"
            + _wrappable_escape(entry["message"]) + "</p>"))
        decision = entry.get("decision")
        callback = entry.get("_timeout_callback")
        if decision is None and callback is not None:
            row = QtGui.QHBoxLayout()
            retry = QtGui.QPushButton("Retry same request")
            stop = QtGui.QPushButton("Stop")
            retry.clicked.connect(
                lambda _checked=False: callback(True))
            stop.clicked.connect(
                lambda _checked=False: callback(False))
            row.addWidget(retry)
            row.addWidget(stop)
            layout.addLayout(row)
        else:
            text = (
                "Retrying the same request…"
                if decision is True else
                "Stopped. The conversation context was preserved.")
            layout.addWidget(self._rich_label("<i>" + text + "</i>"))
        return box

    def _question_widget(self, entry):
        box = QtGui.QFrame()
        box.setFrameShape(QtGui.QFrame.StyledPanel)
        box.setStyleSheet(
            "QFrame { background-color: palette(alternate-base); "
            "border-radius: 4px; }")
        layout = QtGui.QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.addWidget(self._rich_label(
            "<b>Clarification needed</b><p>"
            + _wrappable_escape(entry["question"]) + "</p>"))
        selected = list(entry.get("answer") or [])
        callback = entry.get("_answer_callback")
        controls = []

        def option_text(option):
            suffix = (
                " (Recommended)"
                if option["id"] == entry.get("recommended_option") else "")
            return option["label"] + suffix

        if entry.get("allow_multiple"):
            for option in entry["options"]:
                check = QtGui.QCheckBox(option_text(option))
                check.setChecked(option["id"] in selected)
                check.setEnabled(callback is not None and not selected)
                controls.append((option, check))
                layout.addWidget(check)
                if option.get("description"):
                    layout.addWidget(self._rich_label(
                        "<div style='margin-left:20px;color:gray;'>"
                        + _wrappable_escape(option["description"]) + "</div>"))
            submit = QtGui.QPushButton("Submit selection")
            submit.setEnabled(callback is not None and not selected)

            def submit_choices():
                values = [
                    option["id"] for option, check in controls
                    if check.isChecked()]
                if values:
                    callback(values)

            submit.clicked.connect(submit_choices)
            layout.addWidget(submit)
        else:
            for option in entry["options"]:
                button = QtGui.QPushButton(option_text(option))
                button.setEnabled(callback is not None and not selected)
                button.clicked.connect(
                    lambda _checked=False, value=option["id"]:
                    callback([value]))
                controls.append((option, button))
                layout.addWidget(button)
                if option.get("description"):
                    layout.addWidget(self._rich_label(
                        "<div style='margin-left:20px;color:gray;'>"
                        + _wrappable_escape(option["description"]) + "</div>"))

        if selected:
            labels = [
                option["label"] for option in entry["options"]
                if option["id"] in selected]
            layout.addWidget(self._rich_label(
                "<b>Selected:</b> " + _wrappable_escape(", ".join(labels))))
        elif callback is None:
            layout.addWidget(self._rich_label(
                "<i>This question is no longer active.</i>"))
        return box

    def _context_widget(self, entry):
        image_count = sum(
            1 for message in entry["messages"]
            if isinstance(message["content"], list)
            for block in message["content"]
            if block.get("type") == "image_url")
        count = len(entry["messages"])
        summary = (
            f"Context sent to model ({count} message"
            f'{"s" if count != 1 else ""}'
            + (f", {image_count} image"
               f'{"s" if image_count != 1 else ""}' if image_count else "")
            + ")")

        def content():
            container = QtGui.QWidget()
            layout = QtGui.QVBoxLayout(container)
            layout.setContentsMargins(16, 0, 0, 0)
            for message in entry["messages"]:
                layout.addWidget(self._rich_label(
                    "<b>" + html.escape(message["role"] or "unknown") + "</b>"))
                value = message["content"]
                if isinstance(value, list):
                    for block in value:
                        if block.get("type") == "text":
                            layout.addWidget(self._rich_label(
                                _wrapped_pre(block.get("text", ""))))
                        elif block.get("type") == "image_url":
                            url = (block.get("image_url") or {}).get("url", "")
                            prefix = "data:image/png;base64,"
                            if url.startswith(prefix):
                                layout.addWidget(self._image_widget(
                                    url[len(prefix):]))
                else:
                    layout.addWidget(self._rich_label(
                        _wrapped_pre(value)))
            return container

        return self._disclosure(summary, content)

    def _image_widget(self, encoded):
        raw = base64.b64decode(encoded.encode("ascii"))
        image = QtGui.QImage.fromData(raw, "PNG")
        return _ResponsiveImageLabel(image)

    def _append_user(self, key, text, images):
        state = self._document_states.get(key)
        if state is None:
            return
        entry = {"kind": "user", "text": text, "images": images}
        state["entries"].append(entry)
        self._add_entry_widget(state, entry)

    def _append(self, key, text):
        """Add a plain (HTML) transcript line. Empty strings are ignored."""
        if text == "":
            return
        state = self._document_states.get(key)
        if state is None:
            return
        entry = {"kind": "text", "html": text}
        state["entries"].append(entry)
        self._add_entry_widget(state, entry)

    def _start_status(self, key):
        """Begin the live 'Working…' indicator (call on the GUI thread)."""
        state = self._document_states[key]
        state["elapsed"] = 0
        entry = {"kind": "status", "text": "Working…"}
        state["status_entry"] = entry
        state["entries"].append(entry)
        self._add_entry_widget(state, entry)
        if not self._status_timer.isActive():
            self._status_timer.start()

    def _tick_status(self):
        for state in self._document_states.values():
            entry = state.get("status_entry")
            cancel_event = state.get("cancel_event")
            if (entry is None or state.get("waiting_for_question")
                    or (cancel_event is not None and cancel_event.is_set())):
                continue
            state["elapsed"] += 1
            entry["text"] = f"Working… ({state['elapsed']}s)"
            entry["label"].setText(
                f'<b>●&nbsp; {html.escape(entry["text"])}</b>')

    def _stop_status(self, key):
        """Remove the live indicator when the turn finishes (GUI thread)."""
        state = self._document_states.get(key)
        if state is None:
            return
        entry = state.get("status_entry")
        if entry is not None:
            try:
                state["entries"].remove(entry)
            except ValueError:
                pass
            widget = entry.get("widget")
            if widget is not None:
                state["layout"].removeWidget(widget)
                widget.deleteLater()
            state["status_entry"] = None
        if not any(s.get("status_entry")
                   for s in self._document_states.values()):
            self._status_timer.stop()

    def _add_reasoning(self, key, reasoning):
        """Add a collapsible 'Thinking' entry holding the model's reasoning."""
        if not reasoning:
            return
        state = self._document_states.get(key)
        if not state:
            return
        state["reason_seq"] += 1
        entry = {
            "kind": "reasoning", "id": state["reason_seq"], "text": reasoning,
        }
        state["entries"].append(entry)
        self._add_entry_widget(state, entry)
        if key == self._document_key:
            self._reason_seq = state["reason_seq"]

    def _add_step(self, key, intent, script, result):
        """Add a collapsible executed step, labelled by its approved intent."""
        state = self._document_states.get(key)
        if not state:
            return
        state["step_seq"] += 1
        entry = {
            "kind": "step", "id": state["step_seq"],
            "intent": intent or "Executed Python step",
            "script": script, "result": result,
        }
        state["entries"].append(entry)
        self._add_entry_widget(state, entry)

    def _add_tool(self, key, tool, summary, details):
        """Show a model tool call immediately, before it runs or returns."""
        state = self._document_states.get(key)
        if not state:
            return
        entry = {
            "kind": "tool", "tool": tool,
            "summary": summary, "details": details,
        }
        state["entries"].append(entry)
        self._add_entry_widget(state, entry)

    def _add_context(self, key, messages):
        """Add a minimized record of context newly sent with an LLM call."""
        state = self._document_states.get(key)
        if not state or not messages:
            return
        state["context_seq"] += 1
        # Copy the shallow message/block structure so later history mutations
        # cannot change what the transcript says was sent.
        copied = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                content = [dict(block) for block in content]
            copied.append({"role": message.get("role", ""), "content": content})
        entry = {
            "kind": "context", "id": state["context_seq"],
            "messages": copied,
        }
        state["entries"].append(entry)
        self._add_entry_widget(state, entry)

    def _ask_intent(self, key, intent, answer, done):
        """GUI-thread slot: show the confirm dialog, store the answer, unblock
        the worker. Invoked via the intentAsked signal (queued from the worker)."""
        try:
            state = self._document_states.get(key)
            if state is None:
                answer["ok"] = False
                return
            res = QtGui.QMessageBox.question(
                self, "Run this change?", intent,
                QtGui.QMessageBox.Yes | QtGui.QMessageBox.No)
            answer["ok"] = (res == QtGui.QMessageBox.Yes)
        finally:
            done.set()

    def _ask_question(self, key, proposal, answer, done):
        """Render a structured question and unblock the agent on selection."""
        state = self._document_states.get(key)
        if state is None:
            done.set()
            return
        state["waiting_for_question"] = True
        status_entry = state.get("status_entry")
        if status_entry is not None:
            status_entry["text"] = "Waiting for your answer…"
            status_entry["label"].setText(
                "<b>●&nbsp; Waiting for your answer…</b>")
        entry = {
            "kind": "question",
            "question": proposal.question,
            "options": [dict(option) for option in proposal.options],
            "recommended_option": proposal.recommended_option,
            "allow_multiple": proposal.allow_multiple,
            "answer": [],
        }

        def selected(values):
            if done.is_set():
                return
            entry["answer"] = list(values)
            answer["value"] = list(values)
            entry.pop("_answer_callback", None)
            state["waiting_for_question"] = False
            if status_entry is not None:
                status_entry["text"] = (
                    f"Working… ({state['elapsed']}s)")
                status_entry["label"].setText(
                    f"<b>●&nbsp; Working… ({state['elapsed']}s)</b>")
            # Rebuild only this entry so controls become disabled and the
            # persisted selection is visible.
            self._replace_entry_widget(state, entry)
            done.set()

        entry["_answer_callback"] = selected
        state["entries"].append(entry)
        self._add_entry_widget(state, entry)

    def _ask_timeout(self, key, message, answer, done):
        state = self._document_states.get(key)
        if state is None:
            done.set()
            return
        state["waiting_for_question"] = True
        status_entry = state.get("status_entry")
        if status_entry is not None:
            status_entry["text"] = "Waiting after timeout…"
            status_entry["label"].setText(
                "<b>●&nbsp; Waiting after timeout…</b>")
        entry = {
            "kind": "timeout", "message": message, "decision": None,
        }

        def decided(retry):
            if done.is_set():
                return
            entry["decision"] = bool(retry)
            entry.pop("_timeout_callback", None)
            answer["retry"] = bool(retry)
            state["waiting_for_question"] = False
            if status_entry is not None and retry:
                status_entry["text"] = (
                    f"Working… ({state['elapsed']}s)")
                status_entry["label"].setText(
                    f"<b>●&nbsp; Working… ({state['elapsed']}s)</b>")
            self._replace_entry_widget(state, entry)
            done.set()

        entry["_timeout_callback"] = decided
        state["entries"].append(entry)
        self._add_entry_widget(state, entry)

    def _set_busy(self, key, busy):
        state = self._document_states.get(key)
        if state is None:
            return
        state["busy"] = busy
        if busy:
            self._start_status(key)
        else:
            self._stop_status(key)
            state["waiting_for_question"] = False
            for entry in state["entries"]:
                callback_key = {
                    "question": "_answer_callback",
                    "timeout": "_timeout_callback",
                }.get(entry.get("kind"))
                if callback_key and entry.get(callback_key) is not None:
                    entry.pop(callback_key, None)
                    self._replace_entry_widget(state, entry)
            self._persist_state(state)
            state["cancel_event"] = None
            if key == self._document_key:
                self._update_context_usage()
        self._update_controls()

    def _on_cancel(self):
        """Request cancellation at the next safe agent boundary."""
        state = self._document_states.get(self._document_key)
        if (state is None or not state.get("busy")
                or state.get("cancel_event") is None):
            return
        state["cancel_event"].set()
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("Cancelling…")
        status_entry = state.get("status_entry")
        if status_entry is not None:
            status_entry["text"] = "Cancelling…"
            status_entry["label"].setText(
                "<b>●&nbsp; Cancelling…</b>")

    def _on_attach_images(self):
        paths, _selected = QtGui.QFileDialog.getOpenFileNames(
            self, "Attach images", "",
            "Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp)")
        for path in paths:
            if len(self._pending_images) >= 8:
                QtGui.QMessageBox.warning(
                    self, "Attachment limit",
                    "A maximum of 8 images can be attached to one message.")
                break
            image = QtGui.QImage(path)
            if image.isNull():
                QtGui.QMessageBox.warning(
                    self, "Could not read image",
                    f"FreeCAD could not decode {os.path.basename(path)}.")
                continue
            image = reference_triptych(image)
            encoded = QtCore.QByteArray()
            buffer = QtCore.QBuffer(encoded)
            buffer.open(QtCore.QIODevice.WriteOnly)
            ok = image.save(buffer, "PNG")
            buffer.close()
            if not ok:
                continue
            attachment = {
                "name": os.path.basename(path)
                + " — original | contrast enhanced | edge enhanced",
                "data": base64.b64encode(bytes(encoded)).decode("ascii"),
            }
            self._pending_images.append(attachment)
            self._add_attachment_preview(attachment, image)
        self.attachment_bar.setVisible(bool(self._pending_images))

    def _add_attachment_preview(self, attachment, image):
        row = QtGui.QWidget()
        layout = QtGui.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        preview = QtGui.QPixmap.fromImage(image).scaled(
            40, 40, QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation)
        thumbnail = QtGui.QLabel()
        thumbnail.setPixmap(preview)
        filename = QtGui.QLabel(_wrappable_escape(attachment["name"]))
        filename.setTextFormat(QtCore.Qt.RichText)
        filename.setWordWrap(True)
        remove = QtGui.QPushButton("Remove")
        remove.clicked.connect(
            lambda _checked=False, item=attachment, widget=row:
            self._remove_attachment(item, widget))
        layout.addWidget(thumbnail)
        layout.addWidget(filename, 1)
        layout.addWidget(remove)
        self.attachment_layout.insertWidget(
            self.attachment_layout.count() - 1, row)

    def _remove_attachment(self, attachment, widget):
        try:
            self._pending_images.remove(attachment)
        except ValueError:
            pass
        self.attachment_layout.removeWidget(widget)
        widget.deleteLater()
        self.attachment_bar.setVisible(bool(self._pending_images))

    def _take_pending_images(self):
        images = list(self._pending_images)
        self._pending_images.clear()
        while self.attachment_layout.count() > 1:
            item = self.attachment_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.attachment_bar.setVisible(False)
        return images

    def _on_send(self):
        if getattr(FreeCAD, "ActiveDocument", None) is None:
            return
        self._sync_document()
        key = self._document_key
        state = self._document_states[key]
        if state["busy"]:
            return
        msg = self.input.text().strip()
        if not msg and not self._pending_images:
            return
        if not msg:
            msg = "Please examine the attached image(s)."
        self.input.clear()
        agent = self.agent
        settings = load_settings(FreeCAD.ParamGet(PARAM_PATH))
        agent.settings = settings  # preserve history while picking up preferences
        if (settings.persist_chat_history
                and not state["persistence_loaded"]
                and state["document"] is not None):
            # Enabling persistence while this document is already open can
            # restore its saved history, but never merge stale history into a
            # conversation started while persistence was disabled.
            if not agent.messages and not state["entries"]:
                messages, entries = history_store.load(state["document"])
                agent.messages = messages
                state["entries"] = entries
                self.log.takeWidget()
                old_page = state["page"]
                state["page"], state["layout"] = self._new_transcript_page()
                self.log.setWidget(state["page"])
                self._restore_entry_widgets(state)
                old_page.deleteLater()
            state["persistence_loaded"] = True
        model_label = model_display_name(settings.model)
        user_images = self._take_pending_images()
        self._append_user(key, msg, user_images)
        state["cancel_event"] = threading.Event()
        cancel_event = state["cancel_event"]
        self.cancel_btn.setText("Cancel")
        self._set_busy(key, True)

        def on_intent(intent):
            # Block the worker until the GUI answers, marshaling via a queued
            # signal (reliable from a non-GUI thread, unlike singleShot).
            answer = {}
            done = threading.Event()
            self.intentAsked.emit(key, intent, answer, done)
            done.wait()
            return answer.get("ok", False)

        def on_result(result, snap, intent, script):
            self.stepReady.emit(key, intent, script, result)

        def on_reasoning(reasoning):
            self.reasoningReady.emit(key, reasoning)

        def on_context(messages):
            self.contextReady.emit(key, messages)

        def on_tool(tool, summary, details):
            self.toolReady.emit(key, tool, summary, details)

        def on_question(proposal):
            answer = {}
            done = threading.Event()
            self.questionAsked.emit(key, proposal, answer, done)
            while not done.wait(0.1):
                if cancel_event.is_set():
                    done.set()
                    break
            return answer.get("value", [])

        def on_timeout(message):
            answer = {}
            done = threading.Event()
            self.timeoutAsked.emit(key, message, answer, done)
            while not done.wait(0.1):
                if cancel_event.is_set():
                    done.set()
                    break
            return answer.get("retry", False)

        def work():
            try:
                out = agent.send(
                    msg, on_intent, on_result, on_reasoning, on_context,
                    user_images=user_images, cancel_event=cancel_event,
                    on_question=on_question, on_timeout=on_timeout,
                    on_tool=on_tool)
                self.resultReady.emit(
                    key, f"<b>{html.escape(model_label)}:</b>"
                    + markdown_to_html(out))
            except AgentCancelled:
                self.resultReady.emit(key, "<b>Cancelled.</b>")
            except Exception as e:
                import traceback
                FreeCAD.Console.PrintError(
                    "LLM Copilot error:\n" + traceback.format_exc())
                self.resultReady.emit(
                    key, f"<b>Error:</b> {html.escape(str(e))}")
            finally:
                self.busyChanged.emit(
                    key, False)  # re-enable this document on the GUI thread

        threading.Thread(target=work, daemon=True).start()

    def _on_undo(self):
        if getattr(FreeCAD, "ActiveDocument", None) is None:
            return
        script_executor.undo(FreeCAD)
        self._append(self._document_key, "<i>Undid last change.</i>")
        self._persist_state()

    def _on_clear(self):
        """Clear only the active document's model context and transcript."""
        state = self._document_states.get(self._document_key)
        if (state is None or state.get("busy")
                or getattr(FreeCAD, "ActiveDocument", None) is None):
            return
        answer = QtGui.QMessageBox.question(
            self, "Clear conversation context?",
            "Clear the conversation for this document? "
            "This does not change or undo the FreeCAD model.",
            QtGui.QMessageBox.Yes | QtGui.QMessageBox.No)
        if answer != QtGui.QMessageBox.Yes:
            return
        self._take_pending_images()
        state = self._document_states[self._document_key]
        if state.get("document") is not None:
            history_store.clear(state["document"])
        state["agent"].messages.clear()
        state["entries"].clear()
        state["reason_seq"] = 0
        state["step_seq"] = 0
        state["context_seq"] = 0
        self._reason_seq = 0
        old_page = self.log.takeWidget()
        state["page"], state["layout"] = self._new_transcript_page()
        state["scroll"] = 0
        self.log.setWidget(state["page"])
        if old_page is not None:
            old_page.deleteLater()
        self._update_context_usage()

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
