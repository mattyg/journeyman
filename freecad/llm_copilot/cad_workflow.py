"""Pure helpers for guiding and reviewing a staged, parametric CAD workflow."""

STAGES = ("analyze", "sketch", "additive", "subtractive", "finish", "verify")
STRATEGIES = (
    "part_design", "part", "surface", "modify_existing", "inspection")


def proposal_issues(proposal):
    """Return missing/invalid structured planning fields."""
    issues = []
    if proposal.strategy not in STRATEGIES:
        issues.append("choose a valid CAD strategy")
    if proposal.stage not in STAGES:
        issues.append("choose a valid workflow stage")
    if not proposal.plan:
        issues.append("provide an ordered feature-level plan")
    if not (1 <= proposal.plan_step <= len(proposal.plan)):
        issues.append("identify a valid one-based plan_step")
    if not proposal.success_criteria:
        issues.append("provide measurable success criteria")
    return issues


def review_step(before, after, proposal, settings):
    """Inspect a before/after document state for workflow-specific warnings."""
    old = before.get("objects", {})
    new = after.get("objects", {})
    changed = {
        name for name in new
        if name not in old or new[name] != old.get(name)
    }
    warnings = []

    if settings.sketch_constraint_verification:
        for name in sorted(changed):
            item = new[name]
            if item.get("type") != "Sketcher::SketchObject":
                continue
            if item.get("fully_constrained") is False:
                warnings.append(
                    f"{name} is not fully constrained; inspect remaining "
                    "degrees of freedom or document why they are intentional")
            mode = item.get("properties", {}).get("MapMode")
            if mode in ("", "Deactivated") and proposal.strategy == "part_design":
                warnings.append(
                    f"{name} has no stable attachment; consider an origin plane "
                    "or datum reference")

    created_types = {
        item.get("type", "") for name, item in new.items() if name not in old}
    if settings.parametric_feature_preference:
        if (proposal.strategy == "part_design"
                and "Part::Feature" in created_types):
            warnings.append(
                "The Part Design strategy created an opaque Part::Feature; "
                "prefer an editable sketch/native feature unless justified")

    if settings.stage_order_guidance and changed:
        if proposal.stage == "sketch" and not any(
                new[name].get("type") == "Sketcher::SketchObject"
                for name in changed):
            warnings.append(
                "The sketch stage changed the model without creating a sketch; "
                "confirm that the selected stage is accurate")
        if proposal.stage == "additive" and any(
                "Pocket" in t or "Hole" in t for t in created_types):
            warnings.append(
                "A subtractive feature was created during the additive stage")
        if proposal.stage == "subtractive" and any(
                "Pad" in t or "Additive" in t for t in created_types):
            warnings.append(
                "An additive feature was created during the subtractive stage")
    return warnings


def ledger_text(ledger):
    """Render compact persistent working memory for the next model call."""
    lines = [
        "[design ledger]",
        "Strategy: " + (ledger.get("strategy") or "not chosen"),
        "Current stage: " + (ledger.get("stage") or "analyze"),
    ]
    completed = set(ledger.get("completed_stages", ()))
    plan = ledger.get("plan", ())
    if plan:
        lines.append("Plan:")
        lines.extend(
            f"{'✓' if i < ledger.get('completed_steps', 0) else '○'} {step}"
            for i, step in enumerate(plan))
    criteria = ledger.get("success_criteria", ())
    if criteria:
        lines.append("Success criteria:")
        lines.extend("- " + item for item in criteria)
    if completed:
        lines.append("Stages exercised: " + ", ".join(
            stage for stage in STAGES if stage in completed))
    warnings = ledger.get("warnings", ())
    if warnings:
        lines.append("Open workflow warnings:")
        lines.extend("- " + warning for warning in warnings)
    return "\n".join(lines)
