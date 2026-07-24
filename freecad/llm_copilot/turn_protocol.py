"""The wire protocol between the copilot and the model.

Every message the agent feeds back to the model is a block of text tagged with
a bracketed section header (``[document snapshot]``, ``[executed OK]``,
``[script failed]`` …). That tagged-block vocabulary is the single most
important interface in the system — it is how the model perceives the document
and the outcome of its own actions. This module owns it.

Each function returns the exact string the agent appends to the conversation,
so the wording the model sees is defined in one greppable place and can be
pinned with golden tests. The functions are pure: no FreeCAD, no Qt, no client.
"""

from . import cad_workflow, document_inspector


def request(snapshot, user_message):
    """The user's turn, prefixed with the current document snapshot."""
    return f"[document snapshot]\n{snapshot}\n\n[request]\n{user_message}"


def inspection_result(inspected, *, verify_stage=False):
    """A read-only inspection's result, tagged for the analyze or verify stage."""
    header = (
        "[verify-stage inspection result]\n"
        if verify_stage else "[inspection result]\n")
    return header + inspected


def api_reference(reference):
    """An installed-version FreeCAD API reference lookup result."""
    return "[installed-version API reference]\n" + reference


def automatic_api_reference(reference):
    """An API reference the agent looked up automatically after a script error."""
    return (
        "[automatic installed-version API lookup]\n"
        + reference
        + "\nUse this reference to correct the next script.")


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


def execution_body(result, before, after, new_snapshot, changed_names,
                   settings):
    """The leading feedback for a successful step: status, diagnostics, diff.

    Stops short of the CAD-workflow warnings and design ledger, which depend on
    ledger state the agent updates after this block is built — see
    :func:`workflow_tail`.
    """
    feedback = "[executed OK]\n" + _diagnostics(result)
    if changed_names:
        validation = getattr(result, "validation", "")
        if settings.enhanced_validation:
            feedback += f"[validation]\n{validation}\n"
        if settings.structured_diff:
            feedback += (
                "[document diff]\n"
                + document_inspector.structured_diff(before, after)
                + "\n")
        feedback += f"[new snapshot]\n{new_snapshot}"
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
    if settings.design_ledger_context:
        tail += "\n" + cad_workflow.ledger_text(ledger) + "\n"
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
