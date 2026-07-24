# freecad/llm_copilot/chat_panel.py
import base64
import html
import os
import re
import threading
from PySide import QtGui, QtCore
import FreeCAD
import FreeCADGui as Gui

from . import (
    document_inspector, script_executor, llm_client, view_capture, history_store,
)
from .agent import Agent, AgentCancelled
from .context_usage import format_usage
from .markdown import to_html as markdown_to_html
from .settings import load_settings, model_display_name, PARAM_PATH

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


def _wrappable_escape(text, chunk=48):
    """Escape text and add display-only breaks inside unusually long tokens."""
    parts = []
    for part in re.split(r"(\s+)", str(text or "")):
        if not part or part.isspace():
            parts.append(html.escape(part))
            continue
        parts.append("&#8203;".join(
            html.escape(part[index:index + chunk])
            for index in range(0, len(part), chunk)))
    return "".join(parts)


def _wrapped_pre(text):
    """Monospace, newline-preserving HTML that remains word-wrappable."""
    lines = []
    for line in str(text or "").splitlines():
        leading = len(line) - len(line.lstrip(" "))
        lines.append(
            "&nbsp;" * leading + _wrappable_escape(line[leading:]))
    return ('<div style="font-family:monospace;white-space:normal;'
            'word-wrap:break-word;">'
            + "<br>".join(lines) + "</div>")


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
    intentAsked = QtCore.Signal(str, object, object)
    # Toggle input/button enabled state from the worker thread.
    busyChanged = QtCore.Signal(bool)
    # Model reasoning text captured from the response (worker -> GUI thread).
    reasoningReady = QtCore.Signal(object, str)
    # Completed script step: document key, intent, Python, ExecResult.
    stepReady = QtCore.Signal(object, str, str, object)
    # Newly-added request context about to be sent to the model.
    contextReady = QtCore.Signal(object, object)
    # Structured clarification question: document key, proposal, answer box,
    # completion event.
    questionAsked = QtCore.Signal(object, object, object, object)

    def __init__(self, parent=None):
        super().__init__("LLM Copilot", parent)
        self.setObjectName("LLMCopilotDock")
        body = QtGui.QWidget()
        layout = QtGui.QVBoxLayout(body)
        # Persistent entry widgets: appending or updating one message never
        # rebuilds the rest of the transcript.
        self.log = QtGui.QScrollArea()
        self.log.setWidgetResizable(True)
        self.log.setFrameShape(QtGui.QFrame.NoFrame)
        self.log.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
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
        layout.addLayout(context_row)
        layout.addWidget(self.attachment_bar)
        layout.addWidget(self.input)
        action_row = QtGui.QHBoxLayout()
        action_row.addWidget(self.send_btn)
        action_row.addWidget(self.cancel_btn)
        layout.addLayout(action_row)
        layout.addWidget(self.attach_btn)
        layout.addWidget(self.undo_btn)
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
        self.contextReady.connect(self._add_context, QtCore.Qt.QueuedConnection)
        self.questionAsked.connect(
            self._ask_question, QtCore.Qt.QueuedConnection)
        # Transcript model: list of {"kind": "text"|"reasoning"|"status", ...}.
        # Each FreeCAD document owns an independent agent and transcript.
        self._document_states = {}
        self._document_key = None
        self._entries = []
        self._reason_seq = 0
        self._busy = False
        self._cancel_event = None
        self.input.setStyleSheet(
            "QLineEdit:disabled {"
            " background-color: palette(midlight);"
            " color: palette(mid);"
            " border: 2px solid palette(mid);"
            "}")
        # Live "Working… (Ns)" indicator so a slow model call doesn't look frozen.
        self._status_entry = None
        self._status_widget = None
        self._elapsed = 0
        self._waiting_for_question = False
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

    def _new_agent(self, settings):
        main = self._main

        # Wrap FreeCAD-touching calls so they run on the main thread even though
        # the agent loop runs on a worker thread.
        def inspector(app, rich=False):
            return main.run(lambda: document_inspector.snapshot(app, rich=rich))
        inspector.state = lambda app, rich: main.run(
            lambda: document_inspector.document_state(app, rich=rich))
        inspector.inspect = lambda app, query: main.run(
            lambda: document_inspector.inspect(app, query))
        from . import api_reference
        inspector.api_lookup = lambda app, query, module, symbol: main.run(
            lambda: api_reference.lookup(app, query, module, symbol))

        class _MarshalledExecutor:
            def run(self, app, script, **kwargs):
                return main.run(
                    lambda: script_executor.run(app, script, **kwargs))
            def undo(self, app):
                return main.run(lambda: script_executor.undo(app))

        def capture_views(changed_names, strategy, max_isolated, **options):
            return main.run(lambda: view_capture.capture(
                FreeCAD, changed_names, strategy, max_isolated, **options))

        return Agent(client=_Client(), inspector=inspector,
                     executor=_MarshalledExecutor(), app=FreeCAD,
                     settings=settings, view_capture=capture_views)

    def _active_document_key(self):
        doc = getattr(FreeCAD, "ActiveDocument", None)
        return ("document", id(doc)) if doc is not None else ("no-document",)

    def _sync_document(self):
        """Select (or create) state for the active FreeCAD document."""
        # Keep callbacks and the live status attached to the document where the
        # request started. A document change is picked up as soon as it ends.
        if self._busy:
            return
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
            agent = self._new_agent(settings)
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
        enabled = available and not self._busy
        self.send_btn.setEnabled(enabled)
        self.input.setEnabled(enabled)
        self.attach_btn.setEnabled(enabled)
        self.clear_btn.setEnabled(enabled)
        self.undo_btn.setEnabled(enabled)
        self.cancel_btn.setVisible(self._busy)
        self.cancel_btn.setEnabled(self._busy and self._cancel_event is not None
                                   and not self._cancel_event.is_set())
        self.input.setPlaceholderText(
            ("Working — press Cancel to stop"
             if self._busy else
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
        layout = QtGui.QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)
        layout.addStretch(1)
        return page, layout

    def _is_at_bottom(self):
        bar = self.log.verticalScrollBar()
        return bar.maximum() - bar.value() <= 8

    def _scroll_to_bottom(self):
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _add_entry_widget(self, state, entry):
        follow = (state is self._document_states.get(self._document_key)
                  and self._is_at_bottom())
        widget = self._create_entry_widget(entry)
        insert_at = state["layout"].count() - 1
        # Keep the live Working banner at the end of the transcript while
        # context, reasoning, and script results arrive during the turn.
        status_widget = (
            self._status_entry.get("widget")
            if self._status_entry is not None else None)
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
        return self._context_widget(entry)

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

    def _start_status(self):
        """Begin the live 'Working…' indicator (call on the GUI thread)."""
        self._elapsed = 0
        self._status_entry = {"kind": "status", "text": "Working…"}
        self._entries.append(self._status_entry)
        self._status_widget = self._add_entry_widget(
            self._document_states[self._document_key], self._status_entry)
        self._status_timer.start()

    def _tick_status(self):
        if self._status_entry is None:
            return
        if self._cancel_event is not None and self._cancel_event.is_set():
            return
        if self._waiting_for_question:
            return
        self._elapsed += 1
        self._status_entry["text"] = f"Working… ({self._elapsed}s)"
        self._status_entry["label"].setText(
            f'<b>●&nbsp; {html.escape(self._status_entry["text"])}</b>')

    def _stop_status(self):
        """Remove the live indicator when the turn finishes (GUI thread)."""
        self._status_timer.stop()
        if self._status_entry is not None:
            try:
                self._entries.remove(self._status_entry)
            except ValueError:
                pass
            widget = self._status_entry.get("widget")
            if widget is not None:
                state = self._document_states[self._document_key]
                state["layout"].removeWidget(widget)
                widget.deleteLater()
            self._status_entry = None
            self._status_widget = None

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

    def _ask_question(self, key, proposal, answer, done):
        """Render a structured question and unblock the agent on selection."""
        state = self._document_states.get(key)
        if state is None:
            done.set()
            return
        self._waiting_for_question = True
        if self._status_entry is not None:
            self._status_entry["text"] = "Waiting for your answer…"
            self._status_entry["label"].setText(
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
            self._waiting_for_question = False
            if self._status_entry is not None:
                self._status_entry["text"] = f"Working… ({self._elapsed}s)"
                self._status_entry["label"].setText(
                    f"<b>●&nbsp; Working… ({self._elapsed}s)</b>")
            # Rebuild only this entry so controls become disabled and the
            # persisted selection is visible.
            old_widget = entry.get("widget")
            if old_widget is not None:
                index = state["layout"].indexOf(old_widget)
                replacement = self._question_widget(entry)
                state["layout"].insertWidget(index, replacement)
                state["layout"].removeWidget(old_widget)
                old_widget.deleteLater()
                entry["widget"] = replacement
            done.set()

        entry["_answer_callback"] = selected
        state["entries"].append(entry)
        self._add_entry_widget(state, entry)

    def _set_busy(self, busy):
        self._busy = busy
        if busy:
            self._start_status()
        else:
            completed_state = self._document_states.get(self._document_key)
            self._stop_status()
            self._waiting_for_question = False
            if completed_state is not None:
                for entry in completed_state["entries"]:
                    if (entry.get("kind") == "question"
                            and entry.get("_answer_callback") is not None):
                        entry.pop("_answer_callback", None)
                        old_widget = entry.get("widget")
                        if old_widget is not None:
                            index = completed_state["layout"].indexOf(
                                old_widget)
                            replacement = self._question_widget(entry)
                            completed_state["layout"].insertWidget(
                                index, replacement)
                            completed_state["layout"].removeWidget(old_widget)
                            old_widget.deleteLater()
                            entry["widget"] = replacement
            # Persist the document where the turn began before following any
            # active-document switch caused by the executed script.
            self._persist_state(completed_state)
            self._sync_document()
            self._update_context_usage()
            self._cancel_event = None
        self._update_controls()

    def _on_cancel(self):
        """Request cancellation at the next safe agent boundary."""
        if not self._busy or self._cancel_event is None:
            return
        self._cancel_event.set()
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("Cancelling…")
        if self._status_entry is not None:
            self._status_entry["text"] = "Cancelling…"
            self._status_entry["label"].setText(
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
            if max(image.width(), image.height()) > 1600:
                image = image.scaled(
                    1600, 1600, QtCore.Qt.KeepAspectRatio,
                    QtCore.Qt.SmoothTransformation)
            encoded = QtCore.QByteArray()
            buffer = QtCore.QBuffer(encoded)
            buffer.open(QtCore.QIODevice.WriteOnly)
            ok = image.save(buffer, "PNG")
            buffer.close()
            if not ok:
                continue
            attachment = {
                "name": os.path.basename(path),
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
        if self._busy:
            return
        if getattr(FreeCAD, "ActiveDocument", None) is None:
            return
        msg = self.input.text().strip()
        if not msg and not self._pending_images:
            return
        if not msg:
            msg = "Please examine the attached image(s)."
        self.input.clear()
        self._sync_document()
        key = self._document_key
        agent = self.agent
        settings = load_settings(FreeCAD.ParamGet(PARAM_PATH))
        agent.settings = settings  # preserve history while picking up preferences
        state = self._document_states[key]
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
        self._cancel_event = threading.Event()
        cancel_event = self._cancel_event
        self.cancel_btn.setText("Cancel")
        # _on_send already runs on the GUI thread, so mark busy immediately;
        # this also freezes document selection for the lifetime of the turn.
        self._set_busy(True)

        def on_intent(intent):
            # Block the worker until the GUI answers, marshaling via a queued
            # signal (reliable from a non-GUI thread, unlike singleShot).
            answer = {}
            done = threading.Event()
            self.intentAsked.emit(intent, answer, done)
            done.wait()
            return answer.get("ok", False)

        def on_result(result, snap, intent, script):
            self.stepReady.emit(key, intent, script, result)

        def on_reasoning(reasoning):
            self.reasoningReady.emit(key, reasoning)

        def on_context(messages):
            self.contextReady.emit(key, messages)

        def on_question(proposal):
            answer = {}
            done = threading.Event()
            self.questionAsked.emit(key, proposal, answer, done)
            while not done.wait(0.1):
                if cancel_event.is_set():
                    done.set()
                    break
            return answer.get("value", [])

        def work():
            try:
                out = agent.send(
                    msg, on_intent, on_result, on_reasoning, on_context,
                    user_images=user_images, cancel_event=cancel_event,
                    on_question=on_question)
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
                self.busyChanged.emit(False)  # re-enable input on the GUI thread

        threading.Thread(target=work, daemon=True).start()

    def _on_undo(self):
        if getattr(FreeCAD, "ActiveDocument", None) is None:
            return
        script_executor.undo(FreeCAD)
        self._append(self._document_key, "<i>Undid last change.</i>")
        self._persist_state()

    def _on_clear(self):
        """Clear only the active document's model context and transcript."""
        if self._busy or getattr(FreeCAD, "ActiveDocument", None) is None:
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
