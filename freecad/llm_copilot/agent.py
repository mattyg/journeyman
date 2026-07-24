# freecad/llm_copilot/agent.py
import re


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
        self.fidelity_retries = 0
        self.fidelity_clarification = False
        self.fidelity_questions = 0
        self.part_design_retries = 0
        # Consecutive failures of the same plan step with the same error, so a
        # feature that will not build is abandoned rather than retried
        # cosmetically. See _feature_signature.
        self.feature_signature = None
        self.feature_failures = 0
        # The ledger-first turn is injected at most once per turn, so a model
        # that declines to supply a ledger meets the normal gate, not a loop.
        self.ledger_first_requested = False
        self.multi_feature_retries = 0
        self.noop_retries = 0
        # Identical gate rejections in a row. A gate repeating itself verbatim
        # is a deadlock, not correction — see Agent._reject.
        self.last_rejection = None
        self.repeated_rejections = 0
        self.ledger = {
            "strategy": "", "stage": "analyze", "plan": (),
            "success_criteria": (), "completed_stages": set(),
            "completed_steps": 0, "warnings": (),
        }


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

    def _pre_execution_checks(self, proposal, turn, diagnostic, ledger):
        """Every pre-execution objection to a proposal, collected in one pass.

        Returns ``(blocking, advisories, abort)``. Only three rules block: they
        are the ones where letting the step run causes damage that is expensive
        to undo. Everything else is bookkeeping — the document is transactional
        and the completion gates still enforce it once, at the end, so blocking
        a whole script over metadata only costs a turn and produces nothing.

        ``abort`` is a summary string when a rule exhausted its retry budget.
        Ordering matters: the ledger merges mutate ``turn.ledger`` and the
        clarification check depends on a successful merge, so the sequence here
        matches the order the rules were originally applied.
        """
        from . import cad_workflow, turn_protocol
        blocking, advisories = [], []
        limit = max(1, self.settings.self_correction_attempts)

        # BLOCKING: an inert block makes the model believe constraints exist
        # that do not. It corrupts the model's reasoning; nothing downstream
        # catches that.
        noop_issues = cad_workflow.noop_block_issues(proposal.script)
        if noop_issues:
            turn.noop_retries += 1
            if turn.noop_retries > limit:
                return (), (), (
                    "I couldn't get a script without placeholder blocks: "
                    + "; ".join(noop_issues) + ".")
            blocking.append(turn_protocol.placeholder_code(noop_issues))
        else:
            turn.noop_retries = 0

        # BLOCKING: Part primitives put non-editable geometry in the document.
        if not diagnostic and proposal.strategy != "part_design":
            turn.part_design_retries += 1
            if turn.part_design_retries > limit:
                return (), (), (
                    "I couldn't produce a native Part Design proposal.")
            blocking.append(turn_protocol.part_design_required())

        # Advisory: a plan is metadata; a missing one cannot damage the model.
        if self.settings.structured_cad_planning and not diagnostic:
            issues = cad_workflow.proposal_issues(proposal)
            if issues:
                advisories.extend(issues)
            else:
                ledger.update({
                    "strategy": proposal.strategy, "stage": proposal.stage,
                    "plan": proposal.plan, "plan_step": proposal.plan_step,
                    "success_criteria": proposal.success_criteria,
                })
        if not (self.settings.structured_cad_planning and not diagnostic):
            # Other workflow switches are independently configurable, so retain
            # any optional metadata even when plan enforcement is off.
            for key, value in (
                    ("strategy", proposal.strategy), ("stage", proposal.stage),
                    ("plan", proposal.plan),
                    ("success_criteria", proposal.success_criteria)):
                if value:
                    ledger[key] = value

        if self.settings.assumption_ledger and not diagnostic:
            abort = self._assumption_checks(
                proposal, turn, blocking, advisories, limit)
            if abort:
                return (), (), abort
        elif (self.settings.assumption_ledger
              and proposal.assumptions is not None):
            abort = self._assumption_update_checks(
                proposal, turn, advisories, limit)
            if abort:
                return (), (), abort

        # Advisory pre-execution: the *completion* fidelity gate is what
        # actually prevents silent omission, and it fires when the model claims
        # to be done — which is when omission matters.
        if self.settings.fidelity_target == "replica" and not diagnostic:
            features, issues = cad_workflow.fidelity_feature_issues(
                turn.ledger.get("observed_features"),
                proposal.observed_features)
            if not issues:
                if any(row["status"] == "user_approved_omission"
                       for row in features) and turn.fidelity_questions == 0:
                    turn.fidelity_clarification = True
                    issues.append(
                        "ask the user before omitting an observed feature")
                if any(row["status"] == "blocked" for row in features):
                    issues.append(
                        "change construction strategy to implement blocked "
                        "features; difficulty is not permission to omit them")
            if issues:
                advisories.extend(issues)
            if features:
                turn.ledger["observed_features"] = features
                turn.fidelity_clarification = False

        # Advisory: batching hurts diagnosis, not the document.
        if self.settings.one_feature_per_step and not diagnostic:
            advisories.extend(
                cad_workflow.multi_feature_issues(proposal.script))
        return tuple(blocking), tuple(advisories), None

    def _assumption_checks(self, proposal, turn, blocking, advisories, limit):
        """Ledger validation for a turn that has not yet accepted assumptions."""
        from . import cad_workflow, turn_protocol
        if turn.assumptions_accepted:
            # Assumptions were accepted earlier this turn; a later step may
            # still update them, and those updates still need checking.
            if proposal.assumptions is not None:
                return self._assumption_update_checks(
                    proposal, turn, advisories, limit)
            return None
        # Stage 2: ask for assumptions before the first build. Advisory — the
        # request is what matters, and refusing to run without one costs a turn.
        if (not turn.ledger_first_requested
                and turn.ledger.get("assumptions") is None
                and proposal.assumptions is None):
            turn.ledger_first_requested = True
            advisories.append(
                "provide an assumption ledger with this step: every numeric "
                "value not given in the request, with source, confidence, "
                "consequence and status")
            return None
        issues = cad_workflow.assumption_ledger_missing(proposal, turn)
        if not issues and turn.ledger.get("assumptions") is not None:
            advisories.extend(cad_workflow.relabelled_assumptions(
                turn.ledger["assumptions"], proposal.assumptions))
            merged, merge_issues = cad_workflow.merge_assumptions(
                turn.ledger["assumptions"], proposal.assumptions)
            issues.extend(merge_issues)
        else:
            merged = cad_workflow.sort_assumptions(
                dict(row) for row in (proposal.assumptions or ()))
        if issues:
            # Advisory: the tool schema already enforces row shape, so these
            # are rare, and a malformed row cannot damage the document.
            advisories.extend(issues)
        turn.ledger["assumptions"] = merged
        # BLOCKING: a low-confidence, high-consequence value gets baked into
        # every downstream feature. One line now versus a tree rebuild later.
        blocking_rows = cad_workflow.blocking_assumptions(merged)
        if blocking_rows:
            turn.assumption_clarification = True
            blocking.append(
                turn_protocol.assumption_clarification_required(
                    [row["id"] for row in blocking_rows]))
            return None
        if turn.assumption_clarification and turn.assumption_questions == 0:
            blocking.append(
                turn_protocol.assumption_clarification_required())
            return None
        turn.assumptions_accepted = True
        turn.assumption_clarification = False
        turn.assumption_retries = 0
        return None

    def _assumption_update_checks(self, proposal, turn, advisories, limit):
        """Ledger updates after assumptions were accepted."""
        from . import cad_workflow
        issues = cad_workflow.assumption_ledger_missing(
            proposal, type("_Pending", (), {"assumptions_accepted": False})())
        advisories.extend(cad_workflow.relabelled_assumptions(
            turn.ledger.get("assumptions", ()), proposal.assumptions))
        merged, merge_issues = cad_workflow.merge_assumptions(
            turn.ledger.get("assumptions", ()), proposal.assumptions)
        issues.extend(merge_issues)
        if issues:
            advisories.extend(issues)
        turn.ledger["assumptions"] = merged
        return None

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
        self.messages.append({"role": "user", "content": feedback})
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
            if proposal.kind == "inspect" and self.settings.read_only_inspection:
                self._handle_inspect(proposal, turn, on_tool_result)
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

            blocking, step_advisories, abort = self._pre_execution_checks(
                proposal, turn, diagnostic, ledger)
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
