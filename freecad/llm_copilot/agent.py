# freecad/llm_copilot/agent.py


class AgentCancelled(Exception):
    """Raised when the user cancels an in-progress agent turn."""


# Control signals a proposal handler returns to the dispatch loop.
# LOOP  -> the handler consumed the proposal; ask the model again.
# a str -> end the turn now, returning that text to the user.
# EXECUTE -> not a special proposal kind; fall through to run the script.
LOOP = ("loop",)
EXECUTE = ("execute",)


def _model_history(messages):
    """Compact superseded state from histories written by older versions."""
    compact = []
    for index, message in enumerate(messages):
        item = dict(message)
        content = item.get("content")
        if isinstance(content, str):
            if content.startswith("[document snapshot]\n"):
                marker = "\n\n[request]\n"
                if marker in content:
                    content = "[request]\n" + content.split(marker, 1)[1]
            for marker in ("\n[new snapshot]\n", "\n[design ledger]\n"):
                if marker in content:
                    content = content.split(marker, 1)[0].rstrip() + "\n"
            if ("\n(script)\n" in content and index + 1 < len(messages)
                    and str(messages[index + 1].get("content", "")).startswith(
                        "[executed OK]")):
                content = content.split("\n(script)\n", 1)[0].replace(
                    "(intent)", "(executed intent)", 1)
            item["content"] = content
        compact.append(item)
    return compact


class _Turn:
    """Mutable per-turn state threaded through the proposal handlers.

    Bundles what used to be a handful of loose locals in ``Agent.send`` so each
    handler can read and advance them without a long parameter list. The
    handlers own the retry policy for their own proposal kind; the dispatch loop
    only routes.
    """

    def __init__(self):
        self.executed_steps = 0
        self.retries = 0
        self.planning_retries = 0
        self.completion_retries = 0
        self.question_retries = 0
        self.assumption_retries = 0
        self.assumptions_accepted = False
        self.assumption_clarification = False
        self.assumption_questions = 0
        self.ledger = {
            "strategy": "", "stage": "analyze", "plan": (),
            "success_criteria": (), "completed_stages": set(),
            "completed_steps": 0, "warnings": (),
        }


class DocumentAccess:
    """The agent's single, uniform view of the FreeCAD document.

    Wraps a legacy ``inspector`` callable — either a bare ``inspector(app)`` /
    ``inspector(app, rich=...)`` function, optionally carrying ``.state`` /
    ``.inspect`` / ``.api_lookup`` attributes (as ``chat_panel`` builds it), or
    a simple test double. Any method the callable does not provide falls back to
    the real ``document_inspector`` / ``api_reference`` module functions, so the
    agent can call ``snapshot`` / ``state`` / ``inspect`` / ``api_lookup``
    unconditionally rather than probing for capabilities at every use.
    """

    def __init__(self, inspector, app, agent):
        self._inspector = inspector
        self._app = app
        # Read settings off the agent so a mid-conversation settings swap
        # (chat_panel sets agent.settings on each send) stays authoritative.
        self._agent = agent

    @property
    def _rich(self):
        return self._agent.settings.rich_snapshot

    def snapshot(self):
        try:
            return self._inspector(self._app, rich=self._rich)
        except TypeError:
            # A test double may accept only (app); the production inspector
            # accepts the rich keyword.
            return self._inspector(self._app)

    def state(self):
        reader = getattr(self._inspector, "state", None)
        if reader is not None:
            return reader(self._app, self._rich)
        from . import document_inspector
        return document_inspector.document_state(self._app, rich=self._rich)

    def inspect(self, query):
        reader = getattr(self._inspector, "inspect", None)
        if reader is not None:
            return reader(self._app, query)
        from . import document_inspector
        return document_inspector.inspect(self._app, query)

    def api_lookup(self, query, module="FreeCAD", symbol=""):
        reader = getattr(self._inspector, "api_lookup", None)
        if reader is not None:
            return reader(self._app, query, module, symbol)
        from . import api_reference
        return api_reference.lookup(self._app, query, module, symbol)


class Agent:
    def __init__(self, client, inspector, executor, app, settings,
                 view_capture=None):
        self.client = client
        self.executor = executor
        self.app = app
        self.settings = settings
        self.messages = []
        self.view_capture = view_capture
        self.access = DocumentAccess(inspector, app, self)

    def _handle_inspect(self, proposal, turn):
        """Read-only inspection: run the query, feed the result back."""
        from . import turn_protocol
        inspected = self.access.inspect(proposal.query)
        verification_inspection = (
            bool(turn.executed_steps) and self.settings.final_design_review)
        if verification_inspection:
            turn.ledger["stage"] = "verify"
            turn.ledger["completed_stages"].add("verify")
        self.messages.append({
            "role": "assistant",
            "content": f"(read-only inspection) {proposal.query}"})
        self.messages.append({
            "role": "user",
            "content": turn_protocol.inspection_result(
                inspected, verify_stage=verification_inspection)})
        return LOOP

    def _handle_question(self, proposal, turn, on_question, check_cancelled):
        """Structured clarification: validate, ask the user, feed the choice back."""
        if (not proposal.question or len(proposal.options) < 2
                or on_question is None):
            turn.question_retries += 1
            if turn.question_retries > max(
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
            return LOOP
        turn.question_retries = 0
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
            return LOOP
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
        return LOOP

    def _handle_api_lookup(self, proposal, turn):
        """FreeCAD API reference lookup requested by the model."""
        from . import turn_protocol
        reference = self.access.api_lookup(
            proposal.api_query, proposal.api_module, proposal.api_symbol)
        self.messages.append({
            "role": "assistant",
            "content": (
                f"(FreeCAD API lookup) {proposal.api_module}."
                f"{proposal.api_symbol}\n{proposal.api_query}"),
        })
        self.messages.append({
            "role": "user",
            "content": turn_protocol.api_reference(reference),
        })
        return LOOP

    def _handle_completion(self, proposal, turn):
        """A finish/plain response: enforce final verification, else end the turn."""
        verification_missing = (
            self.settings.mandatory_verification and turn.executed_steps
            and (not proposal.verified or not proposal.evidence))
        review_missing = (
            self.settings.final_design_review and turn.executed_steps
            and not proposal.reviewed_plan)
        if verification_missing or review_missing:
            turn.completion_retries += 1
            # A schema-conforming model normally fixes this in one
            # response. Do not burn tokens repeatedly calling finish.
            if turn.completion_retries > 1:
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
            return LOOP
        self.messages.append({"role": "assistant", "content": proposal.text})
        return proposal.text

    def send(self, user_message, on_intent, on_result, on_reasoning=None,
             on_context=None, user_images=None, cancel_event=None,
             on_question=None, on_timeout=None) -> str:
        from . import cad_workflow, turn_protocol
        from .llm_client import LLMTimeoutError

        def check_cancelled():
            if cancel_event is not None and cancel_event.is_set():
                note = "Cancelled by user."
                self.messages.append({"role": "assistant", "content": note})
                raise AgentCancelled(note)

        context_cursor = len(self.messages)
        include_system_context = context_cursor == 0
        check_cancelled()
        snap = self.access.snapshot()
        current_snapshot = snap
        request_text = turn_protocol.request(user_message)
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
        turn = _Turn()
        ledger = turn.ledger  # same dict; a short alias for the execution block
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
                context_messages.append({
                    "role": "user",
                    "content": turn_protocol.current_context(
                        current_snapshot, ledger, self.settings),
                })
                on_context(context_messages)
                context_cursor = len(self.messages)
            check_cancelled()
            try:
                model_messages = _model_history(self.messages)
                model_messages.append({
                    "role": "user",
                    "content": turn_protocol.current_context(
                        current_snapshot, ledger, self.settings),
                })
                proposal = self.client.complete(model_messages, self.settings)
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
                self._handle_inspect(proposal, turn)
                continue
            if proposal.kind == "question":
                if turn.assumption_clarification:
                    if turn.assumption_questions >= 3:
                        turn.assumption_retries += 1
                        if turn.assumption_retries > max(
                                1, self.settings.self_correction_attempts):
                            summary = (
                                "I couldn't resolve the blocking assumptions "
                                "within the clarification limit.")
                            self.messages.append(
                                {"role": "assistant", "content": summary})
                            return summary
                        self.messages.append({
                            "role": "user",
                            "content": (
                                "[assumption clarification limit]\n"
                                "The one-round limit of three questions has "
                                "been reached. Resubmit the script with the "
                                "user selections incorporated."),
                        })
                        continue
                    turn.assumption_questions += 1
                signal = self._handle_question(
                    proposal, turn, on_question, check_cancelled)
                if signal is LOOP:
                    continue
                return signal
            if (proposal.kind == "api_lookup"
                    and self.settings.freecad_api_lookup):
                self._handle_api_lookup(proposal, turn)
                continue
            # A finish (or any non-script response) ends the turn; its text is
            # the message shown to the user. The model can't leak a script here
            # because tool_choice forces it to pick run_freecad_script for work.
            if not proposal.is_tool_call:
                signal = self._handle_completion(proposal, turn)
                if signal is LOOP:
                    continue
                return signal

            if self.settings.structured_cad_planning:
                issues = cad_workflow.proposal_issues(proposal)
                if issues:
                    turn.planning_retries += 1
                    if turn.planning_retries > self.settings.self_correction_attempts:
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
                turn.planning_retries = 0
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

            if (self.settings.assumption_ledger
                    and not turn.assumptions_accepted):
                issues = cad_workflow.assumption_ledger_missing(proposal, turn)
                if not issues and turn.ledger.get("assumptions") is not None:
                    merged, merge_issues = cad_workflow.merge_assumptions(
                        turn.ledger["assumptions"], proposal.assumptions)
                    issues.extend(merge_issues)
                else:
                    merged = tuple(
                        dict(row) for row in (proposal.assumptions or ()))
                if issues:
                    turn.assumption_retries += 1
                    if turn.assumption_retries > max(
                            1, self.settings.self_correction_attempts):
                        summary = (
                            "I couldn't produce a valid assumption ledger: "
                            + "; ".join(issues) + ".")
                        self.messages.append(
                            {"role": "assistant", "content": summary})
                        return summary
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "[assumption ledger required]\n"
                            + "\n".join("- " + issue for issue in issues)
                            + "\nResubmit the script with a corrected ledger; "
                            "the document has not been edited."),
                    })
                    continue
                turn.ledger["assumptions"] = merged
                blocking = cad_workflow.blocking_assumptions(merged)
                if blocking:
                    turn.assumption_clarification = True
                    ids = ", ".join(row["id"] for row in blocking)
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "[assumption clarification required]\n"
                            f"Blocking assumption ids: {ids}. The script was "
                            "not executed. Use ask_user (at most three "
                            "single-question calls total), then resubmit this "
                            "script with the same ids, updated values/status, "
                            "and evidence citing the user's selection."),
                    })
                    continue
                if (turn.assumption_clarification
                        and turn.assumption_questions == 0):
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "[assumption clarification required]\n"
                            "Call ask_user before marking a blocking assumption "
                            "as confirmed. The script was not executed."),
                    })
                    continue
                turn.assumptions_accepted = True
                turn.assumption_clarification = False
                turn.assumption_retries = 0
            elif (self.settings.assumption_ledger
                  and proposal.assumptions is not None):
                issues = cad_workflow.assumption_ledger_missing(
                    proposal, type("_Pending", (), {
                        "assumptions_accepted": False})())
                merged, merge_issues = cad_workflow.merge_assumptions(
                    turn.ledger.get("assumptions", ()), proposal.assumptions)
                issues.extend(merge_issues)
                if issues:
                    turn.assumption_retries += 1
                    if turn.assumption_retries > max(
                            1, self.settings.self_correction_attempts):
                        summary = (
                            "I couldn't update the assumption ledger safely: "
                            + "; ".join(issues) + ".")
                        self.messages.append(
                            {"role": "assistant", "content": summary})
                        return summary
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "[invalid assumption update]\n"
                            + "\n".join("- " + issue for issue in issues)
                            + "\nKeep stable ids and provide evidence for "
                            "changed values or statuses."),
                    })
                    continue
                turn.ledger["assumptions"] = merged
                turn.assumption_retries = 0

            # record the assistant's tool intent and design stage in history
            workflow_line = ""
            if proposal.strategy or proposal.stage:
                workflow_line = (
                    f"\n(strategy) {proposal.strategy or 'unspecified'}"
                    f"\n(stage) {proposal.stage or 'unspecified'}")
            intent_message_index = len(self.messages)
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
            before = self.access.state()
            try:
                result = self.executor.run(
                    self.app, proposal.script,
                    validate=self.settings.enhanced_validation,
                    rollback_on_failure=self.settings.rollback_on_validation_failure)
            except TypeError:
                result = self.executor.run(self.app, proposal.script)
            after = self.access.state()
            new_snap = self.access.snapshot()
            current_snapshot = new_snap
            on_result(result, new_snap, proposal.intent, proposal.script)
            # If cancellation arrived while Python was already executing, the
            # script cannot be interrupted safely. Report its actual result,
            # then stop before making another model request.
            check_cancelled()

            validation_ok = getattr(result, "validation_ok", True)
            if result.ok and validation_ok:
                turn.executed_steps += 1
                turn.retries = 0
                from .view_capture import changed_object_names
                changed_names = changed_object_names(before, after)
                feedback = turn_protocol.execution_body(
                    result, before, after, changed_names, self.settings)
                workflow_warnings = turn_protocol.review_step(
                    before, after, proposal, self.settings)
                if proposal.stage:
                    ledger["completed_stages"].add(proposal.stage)
                if proposal.plan_step:
                    ledger["completed_steps"] = min(
                        len(ledger.get("plan", ())),
                        max(ledger.get("completed_steps", 0),
                            proposal.plan_step))
                ledger["warnings"] = tuple(workflow_warnings)
                feedback += turn_protocol.workflow_tail(
                    workflow_warnings, ledger, self.settings)
                self.messages.append({
                    "role": "user",
                    "content": feedback,
                })
                # Keep the durable event but discard successful script source;
                # the current document and diff are authoritative thereafter.
                self.messages[intent_message_index]["content"] = (
                    f"(executed intent) {proposal.intent}{workflow_line}")
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
                if turn.executed_steps >= self.settings.max_auto_approved_steps:
                    summary = ("Paused after reaching the step limit "
                               f"({self.settings.max_auto_approved_steps}). "
                               "Tell me to continue if this looks right.")
                    self.messages.append({"role": "assistant", "content": summary})
                    return summary
                continue

            # error path
            turn.retries += 1
            if turn.retries >= self.settings.self_correction_attempts:
                summary = ("I couldn't complete this after "
                           f"{turn.retries} attempts. Last error:\n{result.error}")
                self.messages.append({"role": "assistant", "content": summary})
                return summary
            self.messages.append({
                "role": "user",
                "content": turn_protocol.failure_feedback(result),
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
                reference = self.access.api_lookup(
                    "Resolve this script API failure:\n" + result.error[-3000:],
                    module, "")
                self.messages.append({
                    "role": "user",
                    "content": turn_protocol.automatic_api_reference(reference),
                })
