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

        # Reasoning effort — shown only for providers that expose the knob.
        self.reasoningCombo = QtGui.QComboBox()
        for level in llm_client.REASONING_LEVELS:  # off | low | medium | high
            self.reasoningCombo.addItem(level.capitalize(), level)
        self.reasoningCombo.setToolTip(
            "How much the model should reason before answering, if the provider "
            "supports it. Higher can improve quality but is slower/costlier.")
        form.addRow("Reasoning", self.reasoningCombo)
        self._reasoningLabel = form.labelForField(self.reasoningCombo)
        self.reasoningCombo.currentIndexChanged.connect(self._save_reasoning)

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

        conversation = QtGui.QGroupBox("Conversation", self.form)
        cf = QtGui.QVBoxLayout(conversation)
        self.persistHistoryCheck = QtGui.QCheckBox(
            "Persist chat history in FreeCAD documents")
        self.persistHistoryCheck.setToolTip(
            "Embed compressed model context and transcript entries in the "
            ".FCStd file. Clear context removes embedded history even when "
            "this option is disabled.")
        cf.addWidget(self.persistHistoryCheck)
        outer.addWidget(conversation)

        harness = QtGui.QGroupBox("Self-checking harness", self.form)
        hf = QtGui.QVBoxLayout(harness)
        flags = [
            ("validationCheck", "Validate geometry after each script"),
            ("diffCheck", "Send a structured before/after diff"),
            ("verificationCheck", "Require verification evidence before finish"),
            ("inspectionCheck", "Enable read-only document inspection tool"),
            ("viewsCheck", "Send rendered views to vision-capable models"),
            ("rollbackCheck", "Roll back changes that fail validation"),
            ("richSnapshotCheck", "Include rich type-aware document state"),
        ]
        for attr, label in flags:
            widget = QtGui.QCheckBox(label)
            setattr(self, attr, widget)
            hf.addWidget(widget)
        self.viewsCheck.setToolTip(
            "Renders offscreen views without changing the visible camera. "
            "Enable only for models that accept image input.")
        self.edgesCheck = QtGui.QCheckBox(
            "Overlay dark technical edges")
        self.objectColorsCheck = QtGui.QCheckBox(
            "Use stable colors for separate objects")
        self.depthShadingCheck = QtGui.QCheckBox(
            "Use depth-enhanced multi-light shading")
        self.apiLookupCheck = QtGui.QCheckBox(
            "Enable installed-version FreeCAD API lookup")
        self.apiLookupCheck.setToolTip(
            "Lets the model safely inspect modules, symbols, docstrings, and a "
            "bundled field guide for this installed FreeCAD version. Also "
            "runs automatically after likely API errors.")
        hf.addWidget(self.edgesCheck)
        hf.addWidget(self.objectColorsCheck)
        hf.addWidget(self.depthShadingCheck)
        hf.addWidget(self.apiLookupCheck)
        render_form = QtGui.QFormLayout()
        self.renderStrategyCombo = QtGui.QComboBox()
        self.renderStrategyCombo.addItem("Global views only", "global")
        self.renderStrategyCombo.addItem("Changed elements only", "changed")
        self.renderStrategyCombo.addItem(
            "Global + changed elements", "global_and_changed")
        render_form.addRow("Image capture", self.renderStrategyCombo)
        self.maxIsolatedSpin = QtGui.QSpinBox()
        self.maxIsolatedSpin.setRange(0, 20)
        self.maxIsolatedSpin.setToolTip(
            "Maximum changed final objects rendered independently per step.")
        render_form.addRow("Max isolated elements", self.maxIsolatedSpin)
        hf.addLayout(render_form)
        outer.addWidget(harness)

        workflow = QtGui.QGroupBox("Structured CAD workflow", self.form)
        wf = QtGui.QVBoxLayout(workflow)
        workflow_flags = [
            ("planningCheck", "Require a structured design plan before editing"),
            ("parametricCheck", "Prefer editable parametric features"),
            ("sketchCheck", "Check sketch constraints and attachment"),
            ("stageCheck", "Guide sketch, additive, subtractive, finish order"),
            ("ledgerCheck", "Send a compact design ledger on each iteration"),
            ("finalReviewCheck", "Require a final review against the plan"),
        ]
        for attr, label in workflow_flags:
            widget = QtGui.QCheckBox(label)
            setattr(self, attr, widget)
            wf.addWidget(widget)
        workflow.setToolTip(
            "Independent experimental controls that encourage a human-like, "
            "editable CAD design process.")
        outer.addWidget(workflow)

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
        self.persistHistoryCheck.setChecked(
            p.GetBool("PersistChatHistory", True))
        for widget, key, default in self._harness_controls():
            widget.setChecked(p.GetBool(key, default))
        for widget, key, default in self._workflow_controls():
            widget.setChecked(p.GetBool(key, default))
        strategy = p.GetString("RenderStrategy", "global_and_changed")
        strategy_index = self.renderStrategyCombo.findData(strategy)
        self.renderStrategyCombo.setCurrentIndex(
            strategy_index if strategy_index >= 0 else 2)
        self.maxIsolatedSpin.setValue(p.GetInt("MaxIsolatedImages", 4))
        effort = p.GetString("ReasoningEffort", "off")
        ri = self.reasoningCombo.findData(effort)
        if ri >= 0:
            self.reasoningCombo.blockSignals(True)
            self.reasoningCombo.setCurrentIndex(ri)
            self.reasoningCombo.blockSignals(False)
        self._sync_provider_fields(provider, fetch=True)

    def saveSettings(self):
        p = self._param
        p.SetBool("ConfirmBeforeRunning", self.confirmCheck.isChecked())
        p.SetBool("AutoApproveLoop", self.autoApproveCheck.isChecked())
        p.SetInt("MaxAutoApprovedSteps", self.maxStepsSpin.value())
        p.SetInt("SelfCorrectionAttempts", self.retriesSpin.value())
        p.SetBool("PersistChatHistory",
                  self.persistHistoryCheck.isChecked())
        for widget, key, _default in self._harness_controls():
            p.SetBool(key, widget.isChecked())
        for widget, key, _default in self._workflow_controls():
            p.SetBool(key, widget.isChecked())
        p.SetString("RenderStrategy", self.renderStrategyCombo.currentData())
        p.SetInt("MaxIsolatedImages", self.maxIsolatedSpin.value())
        # provider/key/host/model are saved eagerly on edit; persist current
        # values here too so an OK click is always faithful.
        self._save_key()
        self._save_host()
        self._save_model()
        self._save_reasoning()

    # ---- provider/model plumbing ----

    def _current_provider(self):
        return self.providerCombo.currentData()

    def _harness_controls(self):
        return [
            (self.validationCheck, "EnhancedValidation", True),
            (self.diffCheck, "StructuredDiff", True),
            (self.verificationCheck, "MandatoryVerification", True),
            (self.inspectionCheck, "ReadOnlyInspection", True),
            (self.viewsCheck, "RenderedViews", False),
            (self.rollbackCheck, "RollbackOnValidationFailure", True),
            (self.richSnapshotCheck, "RichSnapshot", True),
            (self.edgesCheck, "TechnicalEdgeOverlay", True),
            (self.objectColorsCheck, "ColorSeparateObjects", True),
            (self.depthShadingCheck, "DepthEnhancedShading", True),
            (self.apiLookupCheck, "FreeCADAPILookup", True),
        ]

    def _workflow_controls(self):
        return [
            (self.planningCheck, "StructuredCADPlanning", True),
            (self.parametricCheck, "ParametricFeaturePreference", True),
            (self.sketchCheck, "SketchConstraintVerification", True),
            (self.stageCheck, "StageOrderGuidance", True),
            (self.ledgerCheck, "DesignLedgerContext", True),
            (self.finalReviewCheck, "FinalDesignReview", True),
        ]

    def _sync_provider_fields(self, provider, fetch):
        is_ollama = provider == "ollama"
        self.apiKeyEdit.setVisible(not is_ollama)
        if self._apiKeyLabel is not None:
            self._apiKeyLabel.setVisible(not is_ollama)
        self.hostEdit.setVisible(is_ollama)
        if self._hostLabel is not None:
            self._hostLabel.setVisible(is_ollama)

        # Reasoning control only for providers that expose it.
        has_reasoning = provider in llm_client.PROVIDERS_WITH_REASONING
        self.reasoningCombo.setVisible(has_reasoning)
        if self._reasoningLabel is not None:
            self._reasoningLabel.setVisible(has_reasoning)

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

    def _save_reasoning(self, *_):
        self._param.SetString("ReasoningEffort", self.reasoningCombo.currentData())
