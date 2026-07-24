# freecad/llm_copilot/agent.py


class AgentCancelled(Exception):
    """Raised when the user cancels an in-progress agent turn."""


class Agent:
    def __init__(self, client, inspector, executor, app, settings,
                 view_capture=None):
        self.client = client
        self.inspector = inspector
        self.executor = executor
        self.app = app
        self.settings = settings
        self.messages = []
        self.view_capture = view_capture

    def _snapshot(self):
        try:
            return self.inspector(self.app, rich=self.settings.rich_snapshot)
        except TypeError:
            return self.inspector(self.app)

    def _state(self):
        from . import document_inspector
        state_reader = getattr(self.inspector, "state", None)
        if state_reader:
            return state_reader(self.app, self.settings.rich_snapshot)
        return document_inspector.document_state(
            self.app, rich=self.settings.rich_snapshot)

    def _inspect(self, query):
        from . import document_inspector
        reader = getattr(self.inspector, "inspect", None)
        return (reader(self.app, query) if reader
                else document_inspector.inspect(self.app, query))

    def _api_lookup(self, query, module="FreeCAD", symbol=""):
        from . import api_reference
        reader = getattr(self.inspector, "api_lookup", None)
        return (reader(self.app, query, module, symbol) if reader
                else api_reference.lookup(self.app, query, module, symbol))

    def send(self, user_message, on_intent, on_result, on_reasoning=None,
             on_context=None, user_images=None, cancel_event=None,
             on_question=None, on_timeout=None) -> str:
        from . import document_inspector, cad_workflow
        from .llm_client import LLMTimeoutError

        def check_cancelled():
            if cancel_event is not None and cancel_event.is_set():
                note = "Cancelled by user."
                self.messages.append({"role": "assistant", "content": note})
                raise AgentCancelled(note)

        context_cursor = len(self.messages)
        include_system_context = context_cursor == 0
        check_cancelled()
        snap = self._snapshot()
        request_text = (
            f"[document snapshot]\n{snap}\n\n[request]\n{user_message}")
        if user_images:
            content = [{"type": "text", "text": request_text}]
            for image in user_images:
                content.append({"type": "text",
                                "text": "User attachment: " + image["name"]})
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + image["data"]},
                })
        else:
            content = request_text
        self.messages.append({"role": "user", "content": content})
        executed_steps = 0
        retries = 0
        planning_retries = 0
        completion_retries = 0
        question_retries = 0
        ledger = {
            "strategy": "", "stage": "analyze", "plan": (),
            "success_criteria": (), "completed_stages": set(),
            "completed_steps": 0, "warnings": (),
        }
        while True:
            check_cancelled()
            if on_context is not None and context_cursor < len(self.messages):
                context_messages = list(self.messages[context_cursor:])
                if include_system_context:
                    prompt_reader = getattr(self.client, "system_prompt", None)
                    if prompt_reader is not None:
                        context_messages.insert(0, {
                            "role": "system",
                            "content": prompt_reader(self.settings),
                        })
                    include_system_context = False
                on_context(context_messages)
                context_cursor = len(self.messages)
            check_cancelled()
            try:
                proposal = self.client.complete(self.messages, self.settings)
            except LLMTimeoutError as exc:
                if on_timeout is not None and on_timeout(str(exc)):
                    check_cancelled()
                    continue
                check_cancelled()
                note = (
                    "Stopped after the model request timed out. Your request "
                    "and conversation context have been preserved.")
                self.messages.append(
                    {"role": "assistant", "content": note})
                return note
            # A stdlib urllib request cannot safely be killed from another
            # thread. Discard its response before any script or UI action.
            check_cancelled()
            reasoning = getattr(proposal, "reasoning", "")
            if reasoning and on_reasoning is not None:
                on_reasoning(reasoning)
            if proposal.kind == "inspect" and self.settings.read_only_inspection:
                inspected = self._inspect(proposal.query)
                verification_inspection = (
                    bool(executed_steps) and self.settings.final_design_review)
                if verification_inspection:
                    ledger["stage"] = "verify"
                    ledger["completed_stages"].add("verify")
                self.messages.append({
                    "role": "assistant",
                    "content": f"(read-only inspection) {proposal.query}"})
                self.messages.append({
                    "role": "user",
                    "content": (
                        ("[verify-stage inspection result]\n"
                         if verification_inspection else
                         "[inspection result]\n")
                        + inspected)})
                continue
            if proposal.kind == "question":
                if (not proposal.question or len(proposal.options) < 2
                        or on_question is None):
                    question_retries += 1
                    if question_retries > max(
                            1, self.settings.self_correction_attempts):
                        summary = (
                            "I couldn't construct a valid question for the "
                            "clarification I need.")
                        self.messages.append(
                            {"role": "assistant", "content": summary})
                        return summary
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "[invalid ask_user call]\nProvide a concise question "
                            "with 2–5 options, each having an id, label, and "
                            "description."),
                    })
                    continue
                question_retries = 0
                option_lines = [
                    f"- {option['id']}: {option['label']} — "
                    f"{option['description']}"
                    for option in proposal.options]
                self.messages.append({
                    "role": "assistant",
                    "content": (
                        "(question) " + proposal.question + "\n"
                        + "\n".join(option_lines)),
                })
                selected = on_question(proposal)
                check_cancelled()
                selected = list(selected or [])
                selected_options = [
                    option for option in proposal.options
                    if option["id"] in selected]
                if not selected_options:
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "[no option selected]\nAsk the question again with "
                            "clear, applicable choices."),
                    })
                    continue
                if not proposal.allow_multiple:
                    selected_options = selected_options[:1]
                self.messages.append({
                    "role": "user",
                    "content": (
                        "[user selection]\n"
                        + "\n".join(
                            f"{option['id']}: {option['label']}"
                            for option in selected_options)),
                })
                continue
            if (proposal.kind == "api_lookup"
                    and self.settings.freecad_api_lookup):
                reference = self._api_lookup(
                    proposal.api_query, proposal.api_module,
                    proposal.api_symbol)
                self.messages.append({
                    "role": "assistant",
                    "content": (
                        f"(FreeCAD API lookup) {proposal.api_module}."
                        f"{proposal.api_symbol}\n{proposal.api_query}"),
                })
                self.messages.append({
                    "role": "user",
                    "content": "[installed-version API reference]\n" + reference,
                })
                continue
            # A finish (or any non-script response) ends the turn; its text is
            # the message shown to the user. The model can't leak a script here
            # because tool_choice forces it to pick run_freecad_script for work.
            if not proposal.is_tool_call:
                verification_missing = (
                    self.settings.mandatory_verification and executed_steps
                    and (not proposal.verified or not proposal.evidence))
                review_missing = (
                    self.settings.final_design_review and executed_steps
                    and not proposal.reviewed_plan)
                if verification_missing or review_missing:
                    completion_retries += 1
                    # A schema-conforming model normally fixes this in one
                    # response. Do not burn tokens repeatedly calling finish.
                    if completion_retries > 1:
                        summary = (
                            "I couldn't complete the required final review "
                            "after several attempts. The model must verify the "
                            "finished document and provide concrete evidence "
                            "before this task can be marked complete.")
                        self.messages.append(
                            {"role": "assistant", "content": summary})
                        return summary
                    requirements = []
                    if verification_missing:
                        requirements.append(
                            "set verified=true and cite concrete evidence")
                    if review_missing:
                        requirements.append(
                            "compare every plan step and success criterion with "
                            "the finished model, then set reviewed_plan=true")
                    markers = []
                    if verification_missing:
                        markers.append("[verification required]")
                    if review_missing:
                        markers.append("[design review required]")
                    self.messages.append({
                        "role": "user",
                        "content": (
                            " ".join(markers) + "\nReview the feature tree, "
                            "measurements, validation, diff, and rendered "
                            "evidence; " + "; ".join(requirements)
                            + ". Correct the model if any check fails.")})
                    continue
                self.messages.append({"role": "assistant", "content": proposal.text})
                return proposal.text

            if self.settings.structured_cad_planning:
                issues = cad_workflow.proposal_issues(proposal)
                if issues:
                    planning_retries += 1
                    if planning_retries > self.settings.self_correction_attempts:
                        summary = (
                            "I couldn't produce a complete CAD design plan: "
                            + "; ".join(issues) + ".")
                        self.messages.append(
                            {"role": "assistant", "content": summary})
                        return summary
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "[structured CAD plan required]\n"
                            + "\n".join("- " + issue for issue in issues)
                            + "\nSubmit a corrected run_freecad_script call "
                            "before editing the document."),
                    })
                    continue
                planning_retries = 0
                ledger.update({
                    "strategy": proposal.strategy,
                    "stage": proposal.stage,
                    "plan": proposal.plan,
                    "plan_step": proposal.plan_step,
                    "success_criteria": proposal.success_criteria,
                })
            else:
                # Other workflow switches are independently configurable, so
                # retain any optional metadata even when plan enforcement is off.
                if proposal.strategy:
                    ledger["strategy"] = proposal.strategy
                if proposal.stage:
                    ledger["stage"] = proposal.stage
                if proposal.plan:
                    ledger["plan"] = proposal.plan
                if proposal.success_criteria:
                    ledger["success_criteria"] = proposal.success_criteria

            # record the assistant's tool intent and design stage in history
            workflow_line = ""
            if proposal.strategy or proposal.stage:
                workflow_line = (
                    f"\n(strategy) {proposal.strategy or 'unspecified'}"
                    f"\n(stage) {proposal.stage or 'unspecified'}")
            self.messages.append(
                {"role": "assistant",
                 "content": (
                     f"(intent) {proposal.intent}{workflow_line}"
                     f"\n(script)\n{proposal.script}")})

            gate_needed = (self.settings.confirm_before_running
                           and not self.settings.auto_approve_loop)
            approval_text = proposal.intent
            if self.settings.structured_cad_planning and proposal.plan:
                approval_text += (
                    "\n\nStrategy: " + proposal.strategy.replace("_", " ")
                    + "\nStage: " + proposal.stage
                    + "\n\nPlan:\n"
                    + "\n".join(
                        f"{index}. {item}"
                        for index, item in enumerate(proposal.plan, 1))
                    + "\n\nSuccess criteria:\n"
                    + "\n".join("- " + item
                                for item in proposal.success_criteria))
            if gate_needed and not on_intent(approval_text):
                check_cancelled()
                note = "Cancelled before running."
                self.messages.append({"role": "user", "content": note})
                return note

            check_cancelled()
            before = self._state()
            try:
                result = self.executor.run(
                    self.app, proposal.script,
                    validate=self.settings.enhanced_validation,
                    rollback_on_failure=self.settings.rollback_on_validation_failure)
            except TypeError:
                result = self.executor.run(self.app, proposal.script)
            after = self._state()
            new_snap = self._snapshot()
            on_result(result, new_snap, proposal.intent, proposal.script)
            # If cancellation arrived while Python was already executing, the
            # script cannot be interrupted safely. Report its actual result,
            # then stop before making another model request.
            check_cancelled()

            validation_ok = getattr(result, "validation_ok", True)
            if result.ok and validation_ok:
                executed_steps += 1
                retries = 0
                output = getattr(result, "output", "") or ""
                out_block = f"[script output]\n{output}\n" if output.strip() else ""
                feedback = f"[executed OK]\n{out_block}"
                stderr = getattr(result, "stderr", "") or ""
                warnings = getattr(result, "console_warnings", "") or ""
                console_errors = getattr(result, "console_errors", "") or ""
                if stderr.strip():
                    feedback += f"[standard error]\n{stderr}\n"
                if warnings.strip():
                    feedback += f"[FreeCAD console warnings]\n{warnings}\n"
                if console_errors.strip():
                    feedback += (
                        f"[FreeCAD console errors]\n{console_errors}\n"
                        "Investigate these errors even if the Python script "
                        "returned successfully.\n")
                from .view_capture import changed_object_names
                changed_names = changed_object_names(before, after)
                if changed_names:
                    validation = getattr(result, "validation", "")
                    if self.settings.enhanced_validation:
                        feedback += f"[validation]\n{validation}\n"
                    if self.settings.structured_diff:
                        feedback += (
                            "[document diff]\n"
                            + document_inspector.structured_diff(before, after)
                            + "\n")
                    feedback += f"[new snapshot]\n{new_snap}"
                else:
                    feedback += "[document unchanged]\n"
                workflow_warnings = cad_workflow.review_step(
                    before, after, proposal, self.settings)
                if proposal.stage:
                    ledger["completed_stages"].add(proposal.stage)
                if proposal.plan_step:
                    ledger["completed_steps"] = min(
                        len(ledger.get("plan", ())),
                        max(ledger.get("completed_steps", 0),
                            proposal.plan_step))
                ledger["warnings"] = tuple(workflow_warnings)
                if workflow_warnings:
                    feedback += (
                        "\n[CAD workflow review]\n"
                        + "\n".join("- " + warning
                                    for warning in workflow_warnings)
                        + "\nResolve these warnings or explicitly verify why "
                        "the current construction is intentional.\n")
                if self.settings.design_ledger_context:
                    feedback += "\n" + cad_workflow.ledger_text(ledger) + "\n"
                self.messages.append({
                    "role": "user",
                    "content": feedback,
                })
                if (changed_names and self.settings.rendered_views
                        and self.view_capture):
                    try:
                        images = self.view_capture(
                            changed_names,
                            self.settings.render_strategy,
                            self.settings.max_isolated_images,
                            technical_edges=self.settings.technical_edge_overlay,
                            object_colors=self.settings.color_separate_objects,
                            depth_shading=self.settings.depth_enhanced_shading)
                    except TypeError:
                        # Compatibility with custom/legacy three-argument
                        # capture callbacks.
                        images = self.view_capture(
                            changed_names,
                            self.settings.render_strategy,
                            self.settings.max_isolated_images)
                    if images:
                        content = [{"type": "text", "text":
                                    "[offscreen rendered views after execution]"}]
                        for image in images:
                            if isinstance(image, str):  # legacy/custom capture
                                label, data = "Rendered view", image
                            else:
                                label, data = image["label"], image["data"]
                            content.append({"type": "text", "text": label})
                            content.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64," + data},
                            })
                        self.messages.append({"role": "user", "content": content})
                if executed_steps >= self.settings.max_auto_approved_steps:
                    summary = ("Paused after reaching the step limit "
                               f"({self.settings.max_auto_approved_steps}). "
                               "Tell me to continue if this looks right.")
                    self.messages.append({"role": "assistant", "content": summary})
                    return summary
                continue

            # error path
            retries += 1
            if retries >= self.settings.self_correction_attempts:
                summary = ("I couldn't complete this after "
                           f"{retries} attempts. Last error:\n{result.error}")
                self.messages.append({"role": "assistant", "content": summary})
                return summary
            output = getattr(result, "output", "") or ""
            out_block = f"[script output]\n{output}\n" if output.strip() else ""
            stderr = getattr(result, "stderr", "") or ""
            stderr_block = (
                f"[standard error]\n{stderr}\n" if stderr.strip() else "")
            warnings = getattr(result, "console_warnings", "") or ""
            warning_block = (
                f"[FreeCAD console warnings]\n{warnings}\n"
                if warnings.strip() else "")
            console_errors = getattr(result, "console_errors", "") or ""
            console_error_block = (
                f"[FreeCAD console errors]\n{console_errors}\n"
                if console_errors.strip() else "")
            validation = getattr(result, "validation", "")
            validation_block = (
                f"[validation failed]\n{validation}\n" if validation else "")
            self.messages.append({
                "role": "user",
                "content": (f"[script failed]\n{out_block}{stderr_block}"
                            f"{warning_block}{console_error_block}"
                            f"{result.error}\n"
                            f"{validation_block}"
                            "Fix the script and call the run_freecad_script tool "
                            "again — do not reply in plain text."),
            })
            if (self.settings.freecad_api_lookup
                    and any(marker in result.error for marker in (
                        "AttributeError", "TypeError", "has no attribute",
                        "unknown property", "Unknown property"))):
                module = "FreeCAD"
                for candidate in ("Sketcher", "PartDesign", "Part"):
                    if candidate in result.error or candidate in proposal.script:
                        module = candidate
                        break
                reference = self._api_lookup(
                    "Resolve this script API failure:\n" + result.error[-3000:],
                    module, "")
                self.messages.append({
                    "role": "user",
                    "content": (
                        "[automatic installed-version API lookup]\n"
                        + reference
                        + "\nUse this reference to correct the next script."),
                })
