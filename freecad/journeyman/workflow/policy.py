"""Pure helpers for guiding and reviewing a staged, parametric CAD workflow."""

STAGES = ("analyze", "sketch", "additive", "subtractive", "finish", "verify")
STRATEGIES = ("part_design",)
LEVELS = ("high", "medium", "low")
STATUSES = ("unverified", "user_confirmed", "measured")
FIDELITY_STATUSES = (
    "planned", "implemented", "user_approved_omission", "blocked")


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
        # Row order is presentation, not content: the harness wants severe
        # assumptions read first, which the agent can arrange itself. Rejecting
        # a whole step over it discards working geometry and, because the
        # complaint named no row, deadlocked. See sort_assumptions.
        if status not in STATUSES:
            issues.append(prefix + " has invalid status")
        if "evidence" not in row:
            issues.append(prefix + " needs evidence (empty when unverified)")
        elif status in ("user_confirmed", "measured") and not str(
                row.get("evidence", "")).strip():
            issues.append(prefix + " needs evidence for its status")
    return issues


def sort_assumptions(rows):
    """Order rows most-severe first, so the ledger reads by consequence.

    Stable, so rows of equal consequence keep the order the model chose. Rows
    with an invalid consequence sort last and are caught by validation.
    """
    def rank(row):
        consequence = row.get("consequence") if isinstance(row, dict) else None
        return (LEVELS.index(consequence)
                if consequence in LEVELS else len(LEVELS))

    return tuple(sorted(rows, key=rank))


def blocking_assumptions(assumptions):
    return tuple(
        row for row in assumptions
        if row.get("confidence") == "low"
        and row.get("consequence") == "high"
        and row.get("status") == "unverified")


def relabelled_assumptions(previous, proposed):
    """Rows whose ``name`` was reworded while value and status held steady.

    A relabel is the model describing the same assumption better as its
    understanding sharpens — worth showing, never worth discarding a script
    over. Only a rename *combined* with a value or status change is a
    redefinition; :func:`merge_assumptions` treats that as an issue.
    """
    old = {row["id"]: row for row in (previous or ())}
    notes = []
    for row in proposed or ():
        before = old.get(row.get("id"))
        if before is None:
            continue
        if (row.get("name") != before.get("name")
                and row.get("value") == before.get("value")
                and row.get("status") == before.get("status")):
            notes.append(
                "assumption {} relabelled: \"{}\" -> \"{}\"".format(
                    row.get("id"), before.get("name"), row.get("name")))
    return notes


def merge_assumptions(previous, proposed):
    """Merge rows while preventing silent deletion or unsupported promotion.

    The merged ledger is sorted most-severe first here rather than demanded of
    the model, so presentation order can never block a step. A row may be
    reworded freely; see :func:`relabelled_assumptions`.
    """
    if not previous:
        return sort_assumptions(dict(row) for row in proposed), []
    old = {row["id"]: row for row in previous}
    new = {row.get("id"): row for row in proposed}
    issues = []
    for row_id, before in old.items():
        after = new.get(row_id)
        if after is None:
            issues.append(f"assumption {row_id} was removed")
            continue
        changed = after.get("value") != before.get("value")
        promoted = after.get("status") in ("user_confirmed", "measured")
        renamed = after.get("name") != before.get("name")
        if renamed and (changed or promoted):
            # Rewording a row *while* moving its value or status is how an
            # assumption is silently redefined under a stable id.
            issues.append(
                "assumption {} was redefined: \"{}\" -> \"{}\"".format(
                    row_id, before.get("name"), after.get("name")))
        if (changed or promoted) and not str(after.get("evidence", "")).strip():
            issues.append(f"assumption {row_id} changed without evidence")
    return sort_assumptions(dict(row) for row in proposed), issues


def fidelity_feature_issues(previous, proposed):
    """Validate and merge the replica's observed-feature checklist."""
    if proposed is None or not proposed:
        return (), ["provide at least one observed feature for replica fidelity"]
    issues = []
    seen = set()
    for index, row in enumerate(proposed, 1):
        prefix = f"observed feature {index}"
        if not isinstance(row, dict):
            issues.append(prefix + " must be an object")
            continue
        row_id = str(row.get("id", "")).strip()
        if not row_id:
            issues.append(prefix + " needs a stable id")
        elif row_id in seen:
            issues.append(prefix + " has a duplicate id")
        seen.add(row_id)
        if not str(row.get("description", "")).strip():
            issues.append(prefix + " needs a description")
        status = row.get("status")
        if status not in FIDELITY_STATUSES:
            issues.append(prefix + " has invalid status")
        if "evidence" not in row:
            issues.append(prefix + " needs evidence (empty while planned)")
        elif status in ("implemented", "user_approved_omission") and not str(
                row.get("evidence", "")).strip():
            issues.append(prefix + " needs evidence for its status")
    old = {row["id"]: row for row in (previous or ())}
    new = {row.get("id"): row for row in proposed if isinstance(row, dict)}
    for row_id, before in old.items():
        after = new.get(row_id)
        if after is None:
            issues.append(f"observed feature {row_id} was removed")
        elif after.get("description") != before.get("description"):
            issues.append(f"observed feature {row_id} was renamed")
        elif (before.get("status") == "implemented"
              and after.get("status") != "implemented"):
            issues.append(f"implemented feature {row_id} regressed")
        elif (before.get("status") == "user_approved_omission"
              and after.get("status") not in (
                  "user_approved_omission", "implemented")):
            issues.append(f"approved feature decision {row_id} regressed")
    return tuple(dict(row) for row in proposed if isinstance(row, dict)), issues


def review_step(before, after, proposal, settings):
    """Inspect a before/after document state for workflow-specific warnings."""
    from ..document import DocumentDelta
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


def part_design_issues(before, after):
    """Return hard violations of the native Part Design construction policy."""
    from ..document import DocumentDelta
    delta = DocumentDelta(before, after)
    if not delta.changed_names:
        return []
    objects = after.get("objects", {})
    bodies = {
        name for name, item in objects.items()
        if item.get("type") == "PartDesign::Body"}
    sketches = {
        name for name, item in objects.items()
        if item.get("type") == "Sketcher::SketchObject"
        and item.get("body") in bodies}
    issues = []
    if not bodies:
        issues.append("create geometry inside a native PartDesign::Body")
    if not sketches:
        issues.append(
            "create and attach at least one sketch inside the Part Design Body")
    for name in delta.changed_names:
        item = objects[name]
        type_id = item.get("type", "")
        if type_id.startswith("Part::"):
            issues.append(
                f"{name} uses forbidden Part workbench type {type_id}")
        if type_id in ("PartDesign::Feature", "PartDesign::FeaturePython"):
            issues.append(
                f"{name} is an opaque {type_id}; use a native Part Design feature")
        if ((type_id.startswith("PartDesign::")
             and type_id != "PartDesign::Body")
                or type_id == "Sketcher::SketchObject"):
            if item.get("body") not in bodies:
                issues.append(f"{name} is not contained in a Part Design Body")
    return issues


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
    built = ledger.get("built")
    if built:
        lines.append(built)
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
    features = ledger.get("observed_features", ())
    if features:
        lines.append("Observed replica features:")
        lines.extend(
            "- {id}: {description}; status={status}; evidence={evidence}".format(
                **row)
            for row in features)
    return "\n".join(lines)


_MUTATING_CALLS = frozenset((
    "addObject", "newObject", "removeObject", "addGeometry", "delGeometry",
    "delGeometries", "clearGeometry", "addConstraint", "delConstraint",
    "recompute", "openTransaction", "commitTransaction", "abortTransaction",
    "undo", "redo", "save", "saveAs", "copyObject", "moveObject",
))


def is_read_only_script(script):
    """True when a script only observes the document and cannot change it.

    Diagnosis must never be gated: an agent that cannot inspect after a failure
    is left guessing, which is how a step repeats unchanged. Conservative — any
    assignment to an attribute, any subscript store, any unrecognised mutating
    call, or unparsable source counts as mutating.

    A script must also actually *report* something to qualify. Otherwise an
    inert placeholder (``pass``) would slip past the planning gates on the
    technicality that it changes nothing.
    """
    import ast
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return False
    reports = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = getattr(node, "targets", None) or [node.target]
            for target in targets:
                # Binding a local name is fine; writing through an object is not.
                if not isinstance(target, ast.Name):
                    return False
        elif isinstance(node, (ast.Delete, ast.Global, ast.Nonlocal)):
            return False
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(
                node.func, "id", None)
            if name in _MUTATING_CALLS:
                return False
            if name in ("print", "PrintMessage", "PrintWarning"):
                reports = True
    return reports


def _created_feature_types(script):
    """Part Design feature type ids constructed by newObject/addObject calls."""
    import ast
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return []
    created = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) not in ("newObject", "addObject"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(
                first.value, str):
            continue
        type_id = first.value
        # Sketches, datums and the Body itself are scaffolding for a feature,
        # not features in their own right; a step may carry them.
        if (type_id.startswith("PartDesign::")
                and type_id != "PartDesign::Body"):
            created.append(type_id)
    return created


def multi_feature_issues(script):
    """Reject a script that builds more than one Part Design feature.

    Per-feature verification is the harness's second principle: a step that
    pads, pockets and pockets again cannot say which operation broke when the
    body comes back invalid, and every result after the first failure is noise.
    """
    created = _created_feature_types(script)
    if len(created) <= 1:
        return []
    return [
        "this step builds {} features ({}); build one and verify it before "
        "the next".format(len(created), ", ".join(created))]


def noop_block_issues(script):
    """Flag loops and conditionals whose body does nothing.

    A ``for ... : pass`` where constraints belong is a placeholder the model
    then reasons about as though it ran — the sketch is treated as constrained
    when nothing was added. Cheap to detect, and the failure is otherwise
    invisible until the geometry is wrong.
    """
    import ast
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return []
    issues = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.While, ast.If, ast.With)):
            continue
        body = node.body
        if not body or not all(isinstance(item, ast.Pass) for item in body):
            continue
        kind = type(node).__name__.lower()
        issues.append(
            f"the {kind} block at line {node.lineno} does nothing; write the "
            "statements it should contain or remove it")
    return issues


def buildable_summary(state):
    """A one-line statement of what the document currently holds, or ''.

    Success criteria are prose and cannot be machine-checked, but "you have a
    valid solid of this size" is a fact — and a run that already had a finished
    part spent six further turns degrading it because nothing ever said so.
    """
    solids, volume, invalid = solid_health(state)
    if invalid or not solids:
        return ""
    biggest = None
    for item in (state.get("objects") or {}).values():
        shape = item.get("shape") or {}
        if not shape.get("solids") or shape.get("valid") is False:
            continue
        bbox = item.get("bbox")
        if bbox and (biggest is None or (shape.get("volume") or 0) > biggest[0]):
            biggest = (shape.get("volume") or 0, bbox,
                       shape.get("cylinder_diameters") or [])
    if biggest is None:
        return ""
    _volume, bbox, holes = biggest
    text = (
        "The document holds a valid solid: "
        + " x ".join(f"{value:.1f}" for value in bbox)
        + f" mm, volume {volume:.1f} mm3")
    if holes:
        text += ", cylinder diameters " + ", ".join(
            f"{d:g}" for d in holes)
    return text + ". Compare it with your success criteria before changing it "\
        "further; if it already meets them, finish."


def solid_health(state):
    """Total solids, volume, and validity through ``DocumentState``."""
    from ..document.state import as_document_state
    return as_document_state(state).health()


def regression_issues(best, after):
    """Report a step that succeeded but left the model worse than before.

    Every other guard watches for failure. A run can also be destroyed by a
    sequence of *successful* steps that each degrade the model — chasing a
    cosmetic flag until a working solid is gone. Compare against the healthiest
    state reached this turn, not merely the previous step, so a slow slide is
    caught rather than each small drop looking acceptable.
    """
    if not best:
        return []
    best_solids, best_volume, _ = solid_health(best)
    solids, volume, invalid = solid_health(after)
    issues = []
    if best_solids and solids < best_solids:
        issues.append(
            f"solid count dropped from {best_solids} to {solids} since the "
            "best state this turn")
    elif best_volume and volume < best_volume * 0.5:
        issues.append(
            f"total volume fell from {best_volume:.1f} to {volume:.1f} — "
            "more than half the material disappeared")
    if invalid:
        issues.append(
            "these objects are now invalid: " + ", ".join(sorted(invalid)))
    return issues
