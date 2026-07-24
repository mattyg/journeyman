"""Custom FreeCAD preferences page for the LLM Copilot.

Built in code (not a static .ui) so the Model dropdown can be populated from a
live provider fetch. Follows FreeCAD's preference-page protocol: a class taking
`parent=None`, exposing its widget as `self.form`, with `loadSettings()` and
`saveSettings()` methods. Registered via Gui.addPreferencePage(class, group).

Layout:

    Provider: [dropdown]
    API key:  [password field]        (hidden for Ollama)
    Host:     [line edit]             (shown only for Ollama)
    Model:    [editable dropdown] [Refresh]
    --- Autonomy ---
    [x] Confirm intent before running
    [ ] Auto-approve consecutive steps
    Max auto-approved steps: [spin]
    Self-correction attempts: [spin]

On open the model dropdown is filled from the cached/curated list for the
selected provider, and — if a key (or Ollama host) is present — a live fetch
refreshes it in the background. Changing the provider re-fills and re-fetches.
Refresh forces a live pull. A failed fetch keeps the cached/curated list.
Provider/key/host/model are saved eagerly on change; autonomy settings are
saved on saveSettings() (the dialog OK).
"""

import threading

from PySide import QtCore, QtGui

import FreeCAD

from . import settings as st
from . import llm_client


class _FetchBridge(QtCore.QObject):
    """Carries a worker thread's result back onto the GUI thread.

    A queued signal is reliable inside FreeCAD's modal preferences dialog, where
    QTimer.singleShot(0, ...) is not guaranteed to run in the nested event loop.
    Payload: (provider, models_list, error_or_empty).
    """
    done = QtCore.Signal(str, object, str)


class LLMCopilotPreferencesPage:
    def __init__(self, parent=None):
        self._param = FreeCAD.ParamGet(st.PARAM_PATH)
        self.form = QtGui.QWidget(parent)
        self.form.setWindowTitle("LLM Copilot")
        self._bridge = _FetchBridge(self.form)
        self._bridge.done.connect(self._on_fetch_done)
        self._build_ui()
        self.loadSettings()

    # ---- UI construction ----

    def _build_ui(self):
        outer = QtGui.QVBoxLayout(self.form)

        provider_group = QtGui.QGroupBox("Provider", self.form)
        form = QtGui.QFormLayout(provider_group)

        self.providerCombo = QtGui.QComboBox()
        for p in st.PROVIDERS:
            self.providerCombo.addItem(st.PROVIDER_LABELS[p], p)
        form.addRow("Provider", self.providerCombo)

        self.apiKeyEdit = QtGui.QLineEdit()
        self.apiKeyEdit.setEchoMode(QtGui.QLineEdit.Password)
        self.apiKeyEdit.setPlaceholderText("API key for the selected provider")
        form.addRow("API key", self.apiKeyEdit)
        self._apiKeyLabel = form.labelForField(self.apiKeyEdit)

        self.hostEdit = QtGui.QLineEdit()
        self.hostEdit.setPlaceholderText(st.OLLAMA_DEFAULT_BASE)
        form.addRow("Host", self.hostEdit)
        self._hostLabel = form.labelForField(self.hostEdit)

        modelRow = QtGui.QHBoxLayout()
        self.modelCombo = QtGui.QComboBox()
        self.modelCombo.setEditable(True)  # allow a custom id
        self.modelCombo.setInsertPolicy(QtGui.QComboBox.NoInsert)
        modelRow.addWidget(self.modelCombo, 1)
        self.refreshBtn = QtGui.QPushButton("Refresh")
        modelRow.addWidget(self.refreshBtn)
        form.addRow("Model", modelRow)

        self.statusLabel = QtGui.QLabel("")
        self.statusLabel.setStyleSheet("color: gray;")
        form.addRow("", self.statusLabel)

        outer.addWidget(provider_group)

        autonomy = QtGui.QGroupBox("Autonomy", self.form)
        af = QtGui.QFormLayout(autonomy)
        self.confirmCheck = QtGui.QCheckBox("Confirm intent before running each step")
        af.addRow(self.confirmCheck)
        self.autoApproveCheck = QtGui.QCheckBox("Auto-approve consecutive loop steps")
        af.addRow(self.autoApproveCheck)
        self.maxStepsSpin = QtGui.QSpinBox()
        self.maxStepsSpin.setRange(1, 100)
        af.addRow("Max auto-approved steps", self.maxStepsSpin)
        self.retriesSpin = QtGui.QSpinBox()
        self.retriesSpin.setRange(0, 20)
        af.addRow("Self-correction attempts", self.retriesSpin)
        outer.addWidget(autonomy)

        outer.addStretch(1)

        self.providerCombo.currentIndexChanged.connect(self._on_provider_changed)
        self.refreshBtn.clicked.connect(self._on_refresh_clicked)
        self.apiKeyEdit.editingFinished.connect(self._save_key)
        self.hostEdit.editingFinished.connect(self._save_host)
        self.modelCombo.currentTextChanged.connect(self._save_model)

    # ---- FreeCAD preference-page protocol ----

    def loadSettings(self):
        p = self._param
        provider = st.get_provider(p)
        self._prev_provider = provider
        idx = self.providerCombo.findData(provider)
        if idx >= 0:
            self.providerCombo.blockSignals(True)
            self.providerCombo.setCurrentIndex(idx)
            self.providerCombo.blockSignals(False)
        self.confirmCheck.setChecked(p.GetBool("ConfirmBeforeRunning", True))
        self.autoApproveCheck.setChecked(p.GetBool("AutoApproveLoop", False))
        self.maxStepsSpin.setValue(p.GetInt("MaxAutoApprovedSteps", 5))
        self.retriesSpin.setValue(p.GetInt("SelfCorrectionAttempts", 3))
        self._sync_provider_fields(provider, fetch=True)

    def saveSettings(self):
        p = self._param
        p.SetBool("ConfirmBeforeRunning", self.confirmCheck.isChecked())
        p.SetBool("AutoApproveLoop", self.autoApproveCheck.isChecked())
        p.SetInt("MaxAutoApprovedSteps", self.maxStepsSpin.value())
        p.SetInt("SelfCorrectionAttempts", self.retriesSpin.value())
        # provider/key/host/model are saved eagerly on edit; persist current
        # values here too so an OK click is always faithful.
        self._save_key()
        self._save_host()
        self._save_model()

    # ---- provider/model plumbing ----

    def _current_provider(self):
        return self.providerCombo.currentData()

    def _sync_provider_fields(self, provider, fetch):
        is_ollama = provider == "ollama"
        self.apiKeyEdit.setVisible(not is_ollama)
        if self._apiKeyLabel is not None:
            self._apiKeyLabel.setVisible(not is_ollama)
        self.hostEdit.setVisible(is_ollama)
        if self._hostLabel is not None:
            self._hostLabel.setVisible(is_ollama)

        p = self._param
        self.apiKeyEdit.setText(st.get_api_key(p, provider))
        self.hostEdit.setText(st.get_api_base(p, provider) if is_ollama else "")

        self._populate_models(provider, st.get_cached_models(p, provider))
        if fetch and self._has_credentials(provider):
            self._start_fetch(provider)

    def _has_credentials(self, provider):
        if provider == "ollama":
            return True  # local; try the default/host regardless
        return bool(st.get_api_key(self._param, provider))

    def _populate_models(self, provider, models):
        current = st.get_model_for_provider(self._param, provider)
        # Newest/flagship-first: family tier then natural version order (so
        # claude-opus-4-8 is above -4-7, and -4-10 above -4-8).
        models = st.sort_models(models, provider)
        self.modelCombo.blockSignals(True)
        self.modelCombo.clear()
        self.modelCombo.addItems(models)
        if current:
            i = self.modelCombo.findText(current)
            if i >= 0:
                self.modelCombo.setCurrentIndex(i)
            else:
                self.modelCombo.setEditText(current)
        self.modelCombo.blockSignals(False)

    def _start_fetch(self, provider):
        self.statusLabel.setText("Fetching models…")
        self.refreshBtn.setEnabled(False)
        settings = st.load_settings(self._param)
        bridge = self._bridge

        def work():
            try:
                models = llm_client.list_models(provider, settings)
                err = ""
            except Exception as exc:  # LLMError (incl. timeout) or unexpected
                models, err = [], str(exc)
            bridge.done.emit(provider, models, err)

        threading.Thread(target=work, daemon=True).start()

    def _on_fetch_done(self, provider, models, error):
        """GUI-thread slot: apply a fetch result (or fall back on failure)."""
        self.refreshBtn.setEnabled(True)
        if provider != self._current_provider():
            return
        if error:
            self.statusLabel.setText("Couldn't fetch models (using saved list)")
            FreeCAD.Console.PrintLog(
                "LLM Copilot model fetch failed: %s\n" % error)
            return
        if models:
            st.set_cached_models(self._param, provider, list(models))
            self._populate_models(provider, list(models))
            self.statusLabel.setText("%d models" % len(models))
        else:
            self.statusLabel.setText("No models returned")

    # ---- signal handlers (GUI thread) ----

    def _on_provider_changed(self, _index):
        # Persist the outgoing provider's key/host (which the combo change may
        # not have flushed via editingFinished) before switching targets.
        prev = getattr(self, "_prev_provider", None)
        if prev and prev != "ollama":
            st.set_api_key(self._param, prev, self.apiKeyEdit.text().strip())
        elif prev == "ollama":
            st.set_ollama_base(self._param, self.hostEdit.text().strip())

        provider = self._current_provider()
        self._prev_provider = provider
        st.set_provider(self._param, provider)
        self._sync_provider_fields(provider, fetch=True)

    def _on_refresh_clicked(self):
        provider = self._current_provider()
        self._save_key()
        self._save_host()
        self._start_fetch(provider)

    def _save_key(self):
        provider = self._current_provider()
        if provider != "ollama":
            st.set_api_key(self._param, provider, self.apiKeyEdit.text().strip())

    def _save_host(self):
        if self._current_provider() == "ollama":
            st.set_ollama_base(self._param, self.hostEdit.text().strip())

    def _save_model(self, *_):
        provider = self._current_provider()
        st.set_model_for_provider(self._param, provider,
                                  self.modelCombo.currentText().strip())
