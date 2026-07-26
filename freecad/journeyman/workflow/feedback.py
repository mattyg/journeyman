"""The wire protocol between Journeyman and the model.

Every message the agent feeds back to the model is a block of text tagged with
a bracketed section header (``[document snapshot]``, ``[executed OK]``,
``[script failed]`` …). That tagged-block vocabulary is the single most
important interface in the system — it is how the model perceives the document
and the outcome of its own actions. This module owns it.

Each function returns the exact string the agent appends to the conversation,
so the wording the model sees is defined in one greppable place and can be
pinned with golden tests. The functions are pure: no FreeCAD, no Qt, no client.
"""

from . import policy as cad_workflow
from ..document import state as document_inspector


def request(user_message):
    """A durable user request; current document state is injected separately."""
    return f"[request]\n{user_message}"


def current_context(snapshot, ledger, settings):
    """Replaceable working state injected for one model call, not persisted."""
    text = "[current document]\n" + snapshot
    if settings.design_ledger_context:
        text += "\n\n" + cad_workflow.ledger_text(ledger)
    return text


def inspection_result(inspected, *, verify_stage=False):
    """A read-only inspection's result, tagged for the analyze or verify stage."""
    header = (
        "[verify-stage inspection result]\n"
        if verify_stage else "[inspection result]\n")
    return header + inspected


RENDER_HEADER = "[rendered views]"


def render_label(objects):
    """The header block introducing on-demand rendered views."""
    if objects:
        return f"{RENDER_HEADER} — isolated: {', '.join(objects)}"
    return f"{RENDER_HEADER} — whole document"


def render_empty(objects):
    """Nothing could be rendered: say why, so the model does not retry blindly."""
    if objects:
        return (
            f"{RENDER_HEADER}\nNothing was rendered for: "
            f"{', '.join(objects)}.\nThose names may not exist, may not be "
            "final shape objects, or may have no geometry yet. Check the "
            "current document for the exact object names.")
    return (
        f"{RENDER_HEADER}\nNothing was rendered — the document has no visible "
        "objects with geometry yet.")


def superseded_inspection(query, *, verify_stage=False):
    """A stale inspection's result, dropped in favour of a newer view.

    The full result stays in the durable transcript; the model sees only that
    the inspection happened and what it covered. Keeping that fact — rather
    than dropping the exchange — stops the model re-running an inspection it
    already did, while keeping requests from growing without bound.
    """
    header = (
        "[superseded verify-stage inspection]\n"
        if verify_stage else "[superseded inspection]\n")
    return (
        header
        + f"You inspected: {query}\n"
        + "The full result is omitted here because the document has since "
        "changed or been inspected again. Inspect again if you need it.")


def is_document_changing_result(content):
    """True when this feedback block reports a step that altered the document.

    Reads the same two markers :func:`execution_body` writes, so the rule for
    "the document moved" lives next to the code that states it.
    """
    return (content.startswith("[executed OK]")
            and "[document unchanged]" not in content)


def api_reference(reference):
    """An installed-version FreeCAD API reference lookup result."""
    return "[installed-version API reference]\n" + reference


def automatic_api_reference(reference):
    """An API reference the agent looked up automatically after a script error."""
    return (
        "[automatic installed-version API lookup]\n"
        + reference
        + "\nUse this reference to correct the next script.")


def _gate(tag, issues, instruction):
    """A gate rejection: tag, the specific issues, and what to do next.

    Gates reject a proposal before the document is touched. The returned string
    is fed to the model *and* shown in the transcript verbatim, so the reason a
    step did not run is identical in both places — see :func:`Agent.send`.
    """
    body = "\n".join("- " + issue for issue in issues)
    return f"[{tag}]\n{body}\n{instruction}" if body else f"[{tag}]\n{instruction}"


def blocked(blocking, advisories=()):
    """One message carrying every objection to a proposal, not just the first.

    Surfacing objections one per round makes the model fix a rule, resubmit,
    and meet the next rule — a whole script discarded each time. Advisories ride
    along so a single revision can resolve everything that was irregular.
    """
    text = "\n\n".join(blocking)
    if advisories:
        text += (
            "\n\n[also advisory — these did not block the step]\n"
            + "\n".join("- " + issue for issue in advisories))
    return text


def advisories(issues):
    """Irregularities that did not stop the step from running."""
    if not issues:
        return ""
    return _gate(
        "workflow advisories", issues,
        "The step ran. These did not block it; address them in the next step "
        "or say why the current construction is intentional.")


def part_design_required():
    """The model proposed a non-Part-Design strategy."""
    return _gate(
        "Part Design required", (),
        "Use strategy=part_design. Create editable geometry in a "
        "PartDesign::Body from attached, constrained sketches and native Part "
        "Design features. Part primitives, Part::Feature, Shape assignment, "
        "and boolean shortcuts are not allowed.")


def structured_plan_required(issues):
    """The structured CAD planning fields are missing or invalid."""
    return _gate(
        "structured CAD plan required", issues,
        "Submit a corrected run_freecad_script call before editing the "
        "document.")


def _received_ledger(rows):
    """Echo the ledger as received, so a complaint about it is self-locating.

    A rejection naming a defect without showing the offending data forces the
    model to guess at what arrived — the same failure as a traceback that gives
    a line number but not the line.
    """
    if not rows:
        return ""
    lines = ["Ledger received:"]
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            lines.append(f"  {index}. {row!r}")
            continue
        lines.append(
            "  {}. {} = {} {} (confidence={}, consequence={}, status={})".format(
                index,
                row.get("id") or "<no id>",
                row.get("value"),
                row.get("unit") or "",
                row.get("confidence"),
                row.get("consequence"),
                row.get("status")).rstrip())
    return "\n".join(lines) + "\n"


def assumption_ledger_required(issues, rows=()):
    """The first script's assumption ledger is missing or invalid."""
    return _gate(
        "assumption ledger required", issues,
        _received_ledger(rows)
        + "Resubmit the script with a corrected ledger; the document has not "
        "been edited.")


def assumption_clarification_required(ids=()):
    """Blocking (low-confidence, high-consequence) assumptions need the user."""
    if not ids:
        return _gate(
            "assumption clarification required", (),
            "Call ask_user before marking a blocking assumption as confirmed. "
            "The script was not executed.")
    return _gate(
        "assumption clarification required", (),
        f"Blocking assumption ids: {', '.join(ids)}. The script was not "
        "executed. Use ask_user (at most three single-question calls total), "
        "then resubmit this script with the same ids, updated values/status, "
        "and evidence citing the user's selection.")


def assumption_clarification_limit():
    """The three-question clarification budget is spent."""
    return _gate(
        "assumption clarification limit", (),
        "The one-round limit of three questions has been reached. Resubmit "
        "the script with the user selections incorporated.")


def invalid_assumption_update(issues, rows=()):
    """A later ledger update dropped rows or promoted them without evidence."""
    return _gate(
        "invalid assumption update", issues,
        _received_ledger(rows)
        + "Keep stable ids and provide evidence for changed values or statuses.")


def fidelity_required(issues):
    """The replica feature checklist is missing, regressed, or self-approved."""
    return _gate(
        "replica fidelity required", issues,
        "The script was not executed. Do not simplify because a feature is "
        "difficult; use another CAD strategy or ask_user for explicit "
        "omission approval.")


def placeholder_code(issues):
    """The script contains blocks that do nothing where work was intended."""
    return _gate(
        "placeholder code", issues,
        "Do not submit inert scaffolding: a block that does nothing will be "
        "reasoned about later as though it ran. The document has not been "
        "edited.")


def one_feature_required(issues):
    """The script builds several features, defeating per-feature verification."""
    return _gate(
        "one feature per step", issues,
        "Submit only the next feature, with the sketch and datums it needs. "
        "Verify it built correctly before proposing the following one. The "
        "document has not been edited.")


def ledger_first():
    """Ask for assumptions and a feature-tree plan before any geometry.

    Injected once per turn, before the first constructing script runs. An
    assumption corrected here costs one line; the same assumption corrected
    after the tree is built costs a rebuild (harness Stage 2).
    """
    return _gate(
        "assumptions and plan first", (),
        "Before building, resubmit this step carrying: (1) an assumption "
        "ledger — every numeric value not given in the request, sorted by the "
        "severity of being wrong, with source, confidence, consequence, and "
        "status; and (2) the ordered feature-tree plan you intend to build, "
        "with this script as its first step. Use [] for the ledger only if "
        "the request fixes every dimension. The document has not been edited.")


def repeated_failure(kind, attempts):
    """The same feature has failed the same way ``attempts`` times running."""
    return _gate(
        "repeated failure — change approach", (),
        f"This step has now failed {attempts} times with the same error "
        f"({kind}). Retrying the same construction will not help. Diagnose "
        "before building again: inspect the objects involved with a read-only "
        "script, or state a different construction strategy for this feature.")


def part_design_violation(issues):
    """The script ran, produced non-Part-Design geometry, and was rolled back."""
    return _gate(
        "Part Design violation — step rolled back", issues,
        "Rebuild the step with a Body, attached sketch, and native Part "
        "Design features.")


def _diagnostics(result):
    """Shared stdout / stderr / console decoration for a run result."""
    text = ""
    output = getattr(result, "output", "") or ""
    if output.strip():
        text += f"[script output]\n{output}\n"
    stderr = getattr(result, "stderr", "") or ""
    if stderr.strip():
        text += f"[standard error]\n{stderr}\n"
    warnings = getattr(result, "console_warnings", "") or ""
    if warnings.strip():
        text += f"[FreeCAD console warnings]\n{warnings}\n"
    console_errors = getattr(result, "console_errors", "") or ""
    if console_errors.strip():
        text += (
            f"[FreeCAD console errors]\n{console_errors}\n"
            "Investigate these errors even if the Python script "
            "returned successfully.\n")
    return text


def execution_body(result, before, after, changed_names, settings):
    """The leading feedback for a successful step: status, diagnostics, diff.

    Stops short of the CAD-workflow warnings and design ledger, which depend on
    ledger state the agent updates after this block is built — see
    :func:`workflow_tail`.
    """
    feedback = "[executed OK]\n" + _diagnostics(result)
    if changed_names:
        validation = getattr(result, "validation", "")
        if settings.enhanced_validation:
            feedback += (
                "[validation]\npassed for changed objects"
                + ("" if validation else " (no shape summary)")
                + "\n")
        if settings.structured_diff:
            feedback += (
                "[document diff]\n"
                + document_inspector.structured_diff(before, after)
                + "\n")
    else:
        feedback += "[document unchanged]\n"
    return feedback


def review_step(before, after, proposal, settings):
    """The CAD-workflow warnings for a step (thin pass-through to cad_workflow)."""
    return cad_workflow.review_step(before, after, proposal, settings)


def workflow_tail(workflow_warnings, ledger, settings):
    """The trailing feedback: CAD-workflow warnings and the design ledger.

    Built after the agent has folded this step into ``ledger`` so the rendered
    ledger reflects the completed stage/step.
    """
    tail = ""
    if workflow_warnings:
        tail += (
            "\n[CAD workflow review]\n"
            + "\n".join("- " + warning for warning in workflow_warnings)
            + "\nResolve these warnings or explicitly verify why "
            "the current construction is intentional.\n")
    return tail


def failure_feedback(result):
    """The feedback block for a script step that failed or failed validation."""
    output = getattr(result, "output", "") or ""
    out_block = f"[script output]\n{output}\n" if output.strip() else ""
    stderr = getattr(result, "stderr", "") or ""
    stderr_block = f"[standard error]\n{stderr}\n" if stderr.strip() else ""
    warnings = getattr(result, "console_warnings", "") or ""
    warning_block = (
        f"[FreeCAD console warnings]\n{warnings}\n" if warnings.strip() else "")
    console_errors = getattr(result, "console_errors", "") or ""
    console_error_block = (
        f"[FreeCAD console errors]\n{console_errors}\n"
        if console_errors.strip() else "")
    validation = getattr(result, "validation", "")
    validation_block = (
        f"[validation failed]\n{validation}\n" if validation else "")
    return (
        f"[script failed]\n{out_block}{stderr_block}"
        f"{warning_block}{console_error_block}"
        f"{result.error}\n"
        f"{validation_block}"
        "Fix the script and call the run_freecad_script tool "
        "again — do not reply in plain text.")
