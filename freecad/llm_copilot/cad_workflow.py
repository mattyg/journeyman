"""Pure helpers for guiding and reviewing a staged, parametric CAD workflow."""

STAGES = ("analyze", "sketch", "additive", "subtractive", "finish", "verify")
STRATEGIES = (
    "part_design", "part", "surface", "modify_existing", "inspection")
LEVELS = ("high", "medium", "low")
STATUSES = ("unverified", "user_confirmed", "measured")


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


def assumption_ledger_missing(proposal, turn_state):
    """Return validation issues for the first script's assumption ledger."""
    if getattr(turn_state, "assumptions_accepted", False):
        return []
    rows = getattr(proposal, "assumptions", None)
    if rows is None:
        return ["provide an assumption ledger (use [] when none are needed)"]
    issues = []
    seen = set()
    previous_rank = -1
    for index, row in enumerate(rows, 1):
        prefix = f"assumption {index}"
        if not isinstance(row, dict):
            issues.append(prefix + " must be an object")
            continue
        row_id = str(row.get("id", "")).strip()
        if not row_id:
            issues.append(prefix + " needs a stable id")
        elif row_id in seen:
            issues.append(prefix + " has a duplicate id")
        seen.add(row_id)
        for field in ("name", "unit", "source", "if_wrong"):
            if not str(row.get(field, "")).strip():
                issues.append(f"{prefix} needs {field}")
        value = row.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(prefix + " value must be numeric")
        confidence = row.get("confidence")
        consequence = row.get("consequence")
        status = row.get("status")
        if confidence not in LEVELS:
            issues.append(prefix + " has invalid confidence")
        if consequence not in LEVELS:
            issues.append(prefix + " has invalid consequence")
        else:
            rank = LEVELS.index(consequence)
            if rank < previous_rank:
                issues.append("assumptions must be sorted high to low consequence")
            previous_rank = rank
        if status not in STATUSES:
            issues.append(prefix + " has invalid status")
        if "evidence" not in row:
            issues.append(prefix + " needs evidence (empty when unverified)")
        elif status in ("user_confirmed", "measured") and not str(
                row.get("evidence", "")).strip():
            issues.append(prefix + " needs evidence for its status")
    return issues


def blocking_assumptions(assumptions):
    return tuple(
        row for row in assumptions
        if row.get("confidence") == "low"
        and row.get("consequence") == "high"
        and row.get("status") == "unverified")


def merge_assumptions(previous, proposed):
    """Merge rows while preventing silent deletion or unsupported promotion."""
    if not previous:
        return tuple(dict(row) for row in proposed), []
    old = {row["id"]: row for row in previous}
    new = {row.get("id"): row for row in proposed}
    issues = []
    for row_id, before in old.items():
        after = new.get(row_id)
        if after is None:
            issues.append(f"assumption {row_id} was removed")
            continue
        if after.get("name") != before.get("name"):
            issues.append(f"assumption {row_id} was renamed")
        changed = after.get("value") != before.get("value")
        promoted = after.get("status") in ("user_confirmed", "measured")
        if (changed or promoted) and not str(after.get("evidence", "")).strip():
            issues.append(f"assumption {row_id} changed without evidence")
    return tuple(dict(row) for row in proposed), issues


def review_step(before, after, proposal, settings):
    """Inspect a before/after document state for workflow-specific warnings."""
    from .document_inspector import DocumentDelta
    delta = DocumentDelta(before, after)
    new = delta.new
    changed = set(delta.changed_names)
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

    created_types = delta.created_types()
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
    assumptions = ledger.get("assumptions", ())
    if assumptions:
        lines.append("Assumptions:")
        lines.extend(
            "- {id}: {name}={value} {unit}; source={source}; "
            "confidence={confidence}; consequence={consequence}; "
            "status={status}; evidence={evidence}".format(**row)
            for row in assumptions)
    return "\n".join(lines)
