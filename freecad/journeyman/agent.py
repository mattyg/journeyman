# freecad/journeyman/agent.py
import re

from .workflow_engine import TurnState as _Turn, WorkflowEngine
from .transcript import Transcript, model_history as _model_history


class AgentCancelled(Exception):
    """Raised when the user cancels an in-progress agent turn."""


# Control signals a proposal handler returns to the dispatch loop.
# LOOP  -> the handler consumed the proposal; ask the model again.
# a str -> end the turn now, returning that text to the user.
# EXECUTE -> not a special proposal kind; fall through to run the script.
LOOP = ("loop",)
EXECUTE = ("execute",)


def _image_blocks(header, images):
    """Multi-part content: a header, then a label+image pair per view.

    Shared by the post-execution capture and the on-demand render tool so both
    reach the model in exactly one wire shape.
    """
    content = [{"type": "text", "text": header}]
    for image in images:
        if isinstance(image, str):  # legacy/custom capture
            label, data = "Rendered view", image
        else:
            label, data = image["label"], image["data"]
        content.append({"type": "text", "text": label})
        content.append({
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + data},
        })
    return content


_EXCEPTION_LINE = re.compile(
    r"^(?:\w+\.)*(\w*(?:Error|Exception|Failure))\b", re.MULTILINE)


def _feature_signature(proposal, result):
    """Identify *which feature failed how*, ignoring cosmetic script edits.

    Keyed on the plan step plus the exception type, so retrying the same
    feature with reshuffled numbers still reads as the same failure — the
    pattern that otherwise repeats until the whole-turn budget runs out.
    """
    error = getattr(result, "error", "") or ""
    kinds = _EXCEPTION_LINE.findall(error)
    if getattr(result, "validation", "") and not kinds:
        kind = "ValidationFailure"
    else:
        kind = kinds[-1] if kinds else "UnknownError"
    return (getattr(proposal, "plan_step", 0) or 0, kind)


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

    def render(self, objects=()):
        """Render on request: named objects in isolation, else the document.

        Reuses the post-execution capture callback, so on-demand and automatic
        renders produce identical imagery. Named objects ride the "changed"
        strategy because :func:`view_capture._final_shape_objects` already
        filters to a name set — there is no separate isolation path to add.
        """
        capture = self._agent.view_capture
        if capture is None:
            return []
        names = tuple(objects)
        strategy = "changed" if names else "global"
        settings = self._agent.settings
        # max_isolated caps how many named objects render separately; it must
        # not clip an explicit request, so raise the ceiling to what was asked.
        limit = max(len(names), settings.max_isolated_images)
        try:
            return capture(
                names, strategy, limit,
                technical_edges=settings.technical_edge_overlay,
                object_colors=settings.color_separate_objects,
                depth_shading=settings.depth_enhanced_shading)
        except TypeError:
            # Compatibility with custom/legacy three-argument capture callbacks.
            return capture(names, strategy, limit)


class Agent:
    def __init__(self, client, inspector, executor, app, settings,
                 view_capture=None):
        self.client = client
        self.executor = executor
        self.app = app
        self.settings = settings
        self.transcript = Transcript()
        self.view_capture = view_capture
        self.access = DocumentAccess(inspector, app, self)

    @property
    def messages(self):
        """Compatibility view of the transcript's durable messages."""
        return self.transcript.messages

    @messages.setter
    def messages(self, messages):
        self.transcript.replace_messages(messages)

    def _reject(self, feedback, proposal, on_tool_result=None, turn=None):
        """Reject a proposal: tell the model and show the user the same text.

        Every gate rejection routes through here so the transcript can never
        drift from what the model was actually told. The tool result carries the
        gate block verbatim — the bracketed tag is the only markup, and it reads
        as a label in the UI and as protocol vocabulary to the model.

        Passing ``turn`` also counts identical rejections. A gate that keeps
        emitting the same text is not teaching the model anything, and the
        model cannot escape a rule it has already tried to satisfy — that
        deadlock costs a whole turn and produces no geometry.
        """
        if turn is not None:
            if feedback == turn.last_rejection:
                turn.repeated_rejections += 1
            else:
                turn.last_rejection = feedback
                turn.repeated_rejections = 1
            if turn.repeated_rejections > 1:
                feedback += (
                    "\n\n[identical rejection repeated]\n"
                    f"This is rejection {turn.repeated_rejections} with exactly "
                    "the same text. Re-reading the rule will not resolve it: "
                    "change what the proposal contains, or call ask_user to "
                    "resolve the disagreement.")
        self.messages.append({"role": "user", "content": feedback})
        if on_tool_result is not None:
            on_tool_result(
                "run_freecad_script", getattr(proposal, "intent", ""), feedback)

    def _handle_render(self, proposal, turn, on_tool_result=None):
        """On-demand rendering: capture views now and feed them back.

        Unlike the post-execution capture, this runs *before* the model
        commits to a script, which is where orientation and placement
        ambiguity actually bites. The images are tagged ephemeral so
        :func:`_model_history` drops them once a script moves the document —
        a stale render carries no self-evident contradiction with the current
        snapshot, so it must not outlive the state it depicts.
        """
        from . import turn_protocol
        objects = tuple(proposal.render_objects)
        images = self.access.render(objects)
        label = turn_protocol.render_label(objects)
        self.messages.append({
            "role": "assistant",
            "content": f"(rendered views) {label}"})
        if images:
            content = _image_blocks(label, images)
        else:
            content = turn_protocol.render_empty(objects)
        self.messages.append({
            "role": "user",
            "content": content,
            "ephemeral": "render",
        })
        if on_tool_result is not None:
            on_tool_result(
                "render_views", label,
                label if images else content)
        return LOOP

    def _handle_inspect(self, proposal, turn, on_tool_result=None):
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
        feedback = turn_protocol.inspection_result(
            inspected, verify_stage=verification_inspection)
        # Tagged, not truncated: the full result stays in the durable
        # transcript for the UI and the export, and _model_history decides at
        # send time whether the model still needs it. See _live_inspection_index.
        self.messages.append({
            "role": "user",
            "content": feedback,
            "ephemeral": "inspection",
            "inspection_query": proposal.query,
        })
        if on_tool_result is not None:
            on_tool_result("inspect_document", proposal.query, feedback)
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

    def _handle_api_lookup(self, proposal, turn, on_tool_result=None):
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
        feedback = turn_protocol.api_reference(reference)
        self.messages.append({"role": "user", "content": feedback})
        target = ".".join(
            value for value in
            (proposal.api_module, proposal.api_symbol) if value)
        if on_tool_result is not None:
            on_tool_result(
                "lookup_freecad_api", target or proposal.api_query, feedback)
        return LOOP

    def _handle_completion(self, proposal, turn):
        """A finish/plain response: enforce final verification, else end the turn."""
        verification_missing = (
            self.settings.mandatory_verification and turn.executed_steps
            and (not proposal.verified or not proposal.evidence))
        review_missing = (
            self.settings.final_design_review and turn.executed_steps
            and not proposal.reviewed_plan)
        fidelity_missing = False
        if self.settings.fidelity_target == "replica" and turn.executed_steps:
            features = turn.ledger.get("observed_features", ())
            approved = {
                row["id"] for row in features
                if row.get("status") == "user_approved_omission"}
            unresolved = [
                row["id"] for row in features
                if row.get("status") in ("planned", "blocked")]
            fidelity_missing = (
                not features or unresolved or not proposal.fidelity_met
                or set(proposal.fidelity_omissions) != approved)
        if verification_missing or review_missing or fidelity_missing:
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
            if fidelity_missing:
                requirements.append(
                    "resolve every observed replica feature, set "
                    "fidelity_met=true, and list exactly the stable ids of "
                    "user-approved omissions")
            markers = []
            if verification_missing:
                markers.append("[verification required]")
            if review_missing:
                markers.append("[design review required]")
            if fidelity_missing:
                markers.append("[replica fidelity required]")
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
             on_question=None, on_timeout=None, on_tool=None,
             on_tool_result=None) -> str:
        from . import cad_workflow, turn_protocol
        from .llm_client import LLMRateLimitError, LLMTimeoutError

        def check_cancelled():
            if cancel_event is not None and cancel_event.is_set():
                note = "Cancelled by user."
                self.messages.append({"role": "assistant", "content": note})
                raise AgentCancelled(note)

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
        workflow = WorkflowEngine(self.settings)
        turn = workflow.state
        ledger = workflow.ledger  # same dict; a short alias for execution
        while True:
            check_cancelled()
            # Build the request once and hand the *same* list to the recorder
            # and to the provider. Recording a separately-assembled
            # approximation is how the transcript came to disagree with what
            # was actually sent — and the transcript is the only artefact left
            # to debug a bad turn from.
            model_messages = self.transcript.model_messages()
            model_messages.append({
                "role": "user",
                "content": turn_protocol.current_context(
                    current_snapshot, ledger, self.settings),
            })
            if on_context is not None:
                request = list(model_messages)
                prompt_reader = getattr(self.client, "system_prompt", None)
                if prompt_reader is not None:
                    # The system prompt is a top-level field on the wire, not a
                    # message; surface it as one so the record is complete.
                    request.insert(0, {
                        "role": "system",
                        "content": prompt_reader(self.settings),
                    })
                on_context(request)
            check_cancelled()
            try:
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
            except LLMRateLimitError as exc:
                # Automatic backoff is already spent by the time this arrives.
                # Waiting longer is the only thing that helps, so offer the
                # same retry the user gets on a timeout rather than failing
                # with a raw HTTP error.
                wait = getattr(exc, "retry_after", None)
                prompt = (
                    "The provider is rate limiting or overloaded"
                    + (f"; it asked for {wait:.0f}s." if wait else ".")
                    + " Retry?")
                if on_timeout is not None and on_timeout(prompt):
                    check_cancelled()
                    continue
                check_cancelled()
                note = (
                    "Stopped: the model provider is rate limited or "
                    "overloaded. Any work already completed is saved, and "
                    "your conversation context has been preserved — try again "
                    "shortly.")
                self.messages.append(
                    {"role": "assistant", "content": note})
                return note
            # A stdlib urllib request cannot safely be killed from another
            # thread. Discard its response before any script or UI action.
            check_cancelled()
            reasoning = getattr(proposal, "reasoning", "")
            if reasoning and on_reasoning is not None:
                on_reasoning(reasoning)
            if on_tool is not None:
                if proposal.kind == "script":
                    on_tool(
                        "run_freecad_script",
                        proposal.intent or "Execute FreeCAD Python",
                        proposal.script)
                elif proposal.kind == "inspect":
                    on_tool(
                        "inspect_document", proposal.query, proposal.query)
                elif proposal.kind == "api_lookup":
                    target = ".".join(
                        value for value in
                        (proposal.api_module, proposal.api_symbol) if value)
                    on_tool(
                        "lookup_freecad_api",
                        target or proposal.api_query,
                        proposal.api_query)
                elif proposal.kind == "render":
                    from . import turn_protocol
                    label = turn_protocol.render_label(proposal.render_objects)
                    on_tool("render_views", label, label)
            if proposal.kind == "inspect" and self.settings.read_only_inspection:
                self._handle_inspect(proposal, turn, on_tool_result)
                continue
            if proposal.kind == "render" and self.settings.on_demand_render:
                self._handle_render(proposal, turn, on_tool_result)
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
                            "content":
                                turn_protocol.assumption_clarification_limit(),
                        })
                        continue
                    turn.assumption_questions += 1
                if turn.fidelity_clarification:
                    turn.fidelity_questions += 1
                signal = self._handle_question(
                    proposal, turn, on_question, check_cancelled)
                if signal is LOOP:
                    continue
                return signal
            if (proposal.kind == "api_lookup"
                    and self.settings.freecad_api_lookup):
                self._handle_api_lookup(proposal, turn, on_tool_result)
                continue
            # A finish (or any non-script response) ends the turn; its text is
            # the message shown to the user. The model can't leak a script here
            # because tool_choice forces it to pick run_freecad_script for work.
            if not proposal.is_tool_call:
                signal = self._handle_completion(proposal, turn)
                if signal is LOOP:
                    continue
                return signal

            # A script that only reads the document is diagnosis, not
            # construction: the planning gates have nothing to check, and
            # blocking it would leave the model guessing after a failure.
            diagnostic = cad_workflow.is_read_only_script(proposal.script)

            blocking, step_advisories, abort = workflow.pre_execution_checks(
                proposal, diagnostic)
            if abort:
                self.messages.append({"role": "assistant", "content": abort})
                return abort
            if blocking:
                self._reject(
                    turn_protocol.blocked(blocking, step_advisories),
                    proposal, on_tool_result, turn)
                continue

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
                if on_tool_result is not None:
                    on_tool_result(
                        "run_freecad_script", proposal.intent,
                        "Not executed — declined by user.")
                return note

            check_cancelled()
            before = self.access.state()
            try:
                result = self.executor.run(
                    self.app, proposal.script,
                    validate=self.settings.enhanced_validation,
                    rollback_on_failure=self.settings.rollback_on_validation_failure,
                    # A read-only script has nothing worth rolling back, and its
                    # output is exactly what the next attempt needs.
                    keep_partial_on_error=(
                        self.settings.keep_partial_on_error or diagnostic))
            except TypeError:
                result = self.executor.run(self.app, proposal.script)
            after = self.access.state()
            new_snap = self.access.snapshot()
            current_snapshot = new_snap
            if result.ok and getattr(result, "validation_ok", True):
                part_design_issues = cad_workflow.part_design_issues(
                    before, after)
                if part_design_issues:
                    self.executor.undo(self.app)
                    current_snapshot = self.access.snapshot()
                    # The script ran but was rolled back; still report the
                    # outcome so the UI shows a result for this tool call.
                    result.rolled_back = True
                    on_result(result, current_snapshot,
                              proposal.intent, proposal.script)
                    turn.part_design_retries += 1
                    if turn.part_design_retries > max(
                            1, self.settings.self_correction_attempts):
                        summary = (
                            "I rolled back repeated non-Part-Design geometry: "
                            + "; ".join(part_design_issues) + ".")
                        self.messages.append(
                            {"role": "assistant", "content": summary})
                        return summary
                    self.messages.append({
                        "role": "user",
                        "content": turn_protocol.part_design_violation(
                            part_design_issues),
                    })
                    continue
                turn.part_design_retries = 0
            on_result(result, new_snap, proposal.intent, proposal.script)
            # If cancellation arrived while Python was already executing, the
            # script cannot be interrupted safely. Record its actual result in
            # history so the next turn sees what happened, then stop before
            # making another model request.
            if cancel_event is not None and cancel_event.is_set():
                outcome = (
                    turn_protocol.failure_feedback(result)
                    if not result.ok else
                    (result.output or "(no output)")[:2000])
                self.messages.append({
                    "role": "user",
                    "content": (
                        "[step executed, then cancelled by user]\n" + outcome),
                })
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
                # A step that succeeded can still have made the model worse.
                workflow_warnings.extend(
                    cad_workflow.regression_issues(turn.best_state, after))
                solids, _volume, invalid = cad_workflow.solid_health(after)
                if not invalid and solids >= turn.best_solids:
                    turn.best_state, turn.best_solids = after, solids
                # Say plainly when a valid solid exists, so verification does
                # not drift into open-ended tinkering on a finished part.
                built = cad_workflow.buildable_summary(after)
                ledger["built"] = built
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
                # Pre-execution irregularities travel with the step they
                # describe, so the model sees "this ran, and here is what was
                # irregular about it" rather than losing the script over it.
                if step_advisories:
                    feedback += "\n" + turn_protocol.advisories(
                        step_advisories)
                self.messages.append({
                    "role": "user",
                    "content": feedback,
                })
                # Keep the durable event but discard successful script source
                # unless the user opted to retain it; the current document and
                # diff are authoritative thereafter.
                if not self.settings.keep_script_history:
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
                        self.messages.append({
                            "role": "user",
                            "content": _image_blocks(
                                "[offscreen rendered views after execution]",
                                images)})
                if turn.executed_steps >= self.settings.max_auto_approved_steps:
                    summary = ("Paused after reaching the step limit "
                               f"({self.settings.max_auto_approved_steps}). "
                               "Tell me to continue if this looks right.")
                    self.messages.append({"role": "assistant", "content": summary})
                    return summary
                continue

            # error path
            signature = _feature_signature(proposal, result)
            if signature == turn.feature_signature:
                turn.feature_failures += 1
            else:
                turn.feature_signature = signature
                turn.feature_failures = 1
            # Two independent budgets: the whole-turn correction allowance, and
            # a tighter per-feature cap that fires when the *same* feature keeps
            # failing the *same* way (harness Stage 4). Report whichever binds.
            stuck = turn.feature_failures > self.settings.feature_retry_cap
            turn.retries += 1
            if stuck or turn.retries >= self.settings.self_correction_attempts:
                # Keep the full diagnostics in history so later turns see the
                # same detail the user saw, not just the last error line.
                self.messages.append({
                    "role": "user",
                    "content": turn_protocol.failure_feedback(result),
                })
                if stuck:
                    summary = (
                        "I couldn't build this feature: it failed "
                        f"{turn.feature_failures} times with the same error "
                        f"({signature[1]}). Last error:\n{result.error}")
                else:
                    summary = (
                        "I couldn't complete this after "
                        f"{turn.retries} attempts. Last error:\n{result.error}")
                self.messages.append({"role": "assistant", "content": summary})
                return summary
            self.messages.append({
                "role": "user",
                "content": turn_protocol.failure_feedback(result),
            })
            if turn.feature_failures > 1:
                feedback = turn_protocol.repeated_failure(
                    signature[1], turn.feature_failures)
                self.messages.append({"role": "user", "content": feedback})
                if on_tool_result is not None:
                    on_tool_result(
                        "run_freecad_script", proposal.intent, feedback)
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
