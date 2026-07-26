import io
import re
import traceback
import contextlib
from .types import ExecResult


_WARNING_METHODS = (
    "PrintWarning", "PrintUserWarning", "PrintTranslatedUserWarning",
    "PrintDeveloperWarning",
)
_ERROR_METHODS = (
    "PrintError", "PrintCritical", "PrintUserError",
    "PrintTranslatedUserError", "PrintDeveloperError",
)


@contextlib.contextmanager
def _capture_console(console, warning_buffer, error_buffer):
    """Capture Python-visible FreeCAD console diagnostics without hiding them."""
    originals = {}

    def install(name, target):
        original = getattr(console, name, None)
        if original is None:
            return
        originals[name] = original

        def wrapped(*args, **kwargs):
            target.write(" ".join(str(arg) for arg in args))
            if not target.getvalue().endswith("\n"):
                target.write("\n")
            return original(*args, **kwargs)

        setattr(console, name, wrapped)

    try:
        for name in _WARNING_METHODS:
            install(name, warning_buffer)
        for name in _ERROR_METHODS:
            install(name, error_buffer)
        yield
    finally:
        for name, original in originals.items():
            setattr(console, name, original)


_SCRIPT_FRAME = re.compile(
    r'^(\s*)File "<llm_script>", line (\d+)(?:, in .*)?$')


def _annotate_traceback(script, text, context=2):
    """Splice the offending source lines into ``<llm_script>`` frames.

    A traceback that says only ``line 13`` forces the model to count lines by
    hand to find what failed — which it does unreliably, burning turns on
    misidentified statements. Showing the source at the fault, with a little
    context, makes the error self-locating.
    """
    lines = script.splitlines()
    out = []
    for entry in text.splitlines():
        out.append(entry)
        match = _SCRIPT_FRAME.match(entry)
        if match is None:
            continue
        indent, number = match.group(1), int(match.group(2))
        if not 1 <= number <= len(lines):
            continue
        start = max(1, number - context)
        end = min(len(lines), number + context)
        width = len(str(end))
        for current in range(start, end + 1):
            marker = ">>>" if current == number else "   "
            out.append(
                f"{indent}  {marker} {current:{width}d} | {lines[current - 1]}")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _state_flags(obj):
    """FreeCAD's object State as a list of flag names.

    Real FreeCAD exposes a list; accept a bare string too so a caller passing
    "Invalid" is not silently read character by character.
    """
    state = getattr(obj, "State", None) or ()
    return [state] if isinstance(state, str) else list(state)


def assert_feature(obj, solids=None):
    """Raise a specific error unless ``obj`` built into usable geometry.

    Hand-rolled checks (``pad.Shape.isValid()``) raise an opaque OCC error when
    a feature fails, or silently pass on a null shape — either way the script
    reports the wrong thing about which step broke. This names the failure.

    Pass ``solids`` to also assert an exact solid count.
    """
    if obj is None:
        raise ValueError("assert_feature: no object (the feature was not created)")
    name = getattr(obj, "Name", None) or repr(obj)
    state = _state_flags(obj)
    # 'Touched' alone only means the feature awaits a recompute — not a defect.
    # Recompute and re-read before deciding, so a pending update is not
    # reported as a broken feature.
    if "Touched" in state and "Invalid" not in state:
        document = getattr(obj, "Document", None)
        if document is not None:
            try:
                document.recompute()
            except Exception:
                pass
        state = _state_flags(obj)
    if "Invalid" in state:
        raise ValueError(f"{name} is in an Invalid state: {state}")
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        raise ValueError(
            f"{name} produced a NULL shape — the feature failed to build. "
            "Check that its sketch forms a single closed wire and that the "
            "profile and length are set.")
    if not shape.isValid():
        raise ValueError(f"{name} produced an invalid shape")
    count = len(shape.Solids)
    if solids is not None and count != solids:
        raise ValueError(
            f"{name} produced {count} solids, expected {solids}")
    if solids is None and count == 0:
        raise ValueError(
            f"{name} produced no solids — the feature added no material")
    return obj


_SOLVER_STATUS = {
    -1: "failed to converge",
    -2: "are redundant or conflicting",
    -3: "over-constrain the sketch",
    -4: "over-constrain the sketch",
}


def assert_sketch_constrained(sketch):
    """Raise unless ``sketch`` is closed and fully constrained."""
    name = getattr(sketch, "Name", None) or repr(sketch)
    # An empty sketch is a different problem from one whose shape failed to
    # build, and saying "no shape" for it sends the model looking in the wrong
    # place — it was the terminal error of a whole failed run.
    geometry = getattr(sketch, "Geometry", None)
    if geometry is not None and len(geometry) == 0:
        raise ValueError(
            f"{name} has no geometry — add geometry before constraining it")
    shape = getattr(sketch, "Shape", None)
    if shape is None or shape.isNull():
        raise ValueError(
            f"{name} has no shape: it has {len(geometry or ())} geometry "
            "elements but produced no edges, so the sketch failed to build")
    if shape.Wires and not all(wire.isClosed() for wire in shape.Wires):
        raise ValueError(
            f"{name} is not a closed wire; a pad or pocket profile must close")
    status = sketch.solve() if hasattr(sketch, "solve") else 0
    # solve() returns a solver STATUS, not a count of free parameters: 0 means
    # "solved", and negatives are failures. A conflicting sketch is fixed by
    # REMOVING a constraint, so it must not be reported as "add more".
    if status and status < 0:
        raise ValueError(
            f"{name} could not be solved (status {status}): its constraints "
            f"{_SOLVER_STATUS.get(status, 'conflict with each other')}. "
            "Remove or relax a constraint — adding more will not fix this.")
    if getattr(sketch, "FullyConstrained", None) is False:
        raise ValueError(
            f"{name} is not fully constrained: some geometry can still move. "
            "Add the missing dimensions or relations, then re-check.")
    return sketch


def run(app, script: str, validate=False, rollback_on_failure=False,
        keep_partial_on_error=True) -> "ExecResult":
    doc = app.ActiveDocument
    if doc is None:
        return ExecResult(False, "", "NO_ACTIVE_DOCUMENT")
    g = {
        "App": app, "FreeCAD": app,
        "assert_feature": assert_feature,
        "assert_sketch_constrained": assert_sketch_constrained,
    }
    try:
        import Part
        g["Part"] = Part
    except Exception:
        pass
    # Undo/redo recording is off by default for documents created headlessly
    # (e.g. under freecadcmd); ensure it's on so the transaction below is
    # actually undoable.
    if not doc.UndoMode:
        doc.UndoMode = 1
    before_state = None
    if validate:
        from . import document_inspector
        before_state = document_inspector.document_state(app, rich=True)
    doc.openTransaction("Journeyman")
    stdout = io.StringIO()
    stderr = io.StringIO()
    console_warnings = io.StringIO()
    console_errors = io.StringIO()

    def result(ok, error="", validation_ok=True, validation="",
               rolled_back=False):
        return ExecResult(
            ok, stdout.getvalue(), error, validation_ok, validation,
            rolled_back, stderr.getvalue(), console_warnings.getvalue(),
            console_errors.getvalue())

    try:
        with (contextlib.redirect_stdout(stdout),
              contextlib.redirect_stderr(stderr),
              _capture_console(
                  app.Console, console_warnings, console_errors)):
            exec(compile(script, "<llm_script>", "exec"), g)
            doc.recompute()
            validation_ok, validation = True, ""
            if validate:
                after_state = document_inspector.document_state(app, rich=True)
                changed_names = document_inspector.DocumentDelta(
                    before_state, after_state).changed_names
                validation_ok, validation = document_inspector.validate(
                    app, names=changed_names)
                if not validation_ok and rollback_on_failure:
                    doc.abortTransaction()
                    # Name what broke in the error itself. A bare
                    # POST_EXECUTION_VALIDATION_FAILED tells the model nothing
                    # it can act on, and the detail used to live only in a
                    # separate field the failure feedback renders further down.
                    return result(
                        False,
                        "POST_EXECUTION_VALIDATION_FAILED — the step was "
                        "rolled back because it left the document invalid:\n"
                        + validation
                        + "\nFix the object named above. If a sketch is "
                        "over-constrained, remove a constraint rather than "
                        "adding one.",
                        False, validation, True)
        doc.commitTransaction()
        return result(True, validation_ok=validation_ok, validation=validation)
    except Exception:
        error = _annotate_traceback(script, traceback.format_exc())
        # A script that raises part-way through has usually still done work the
        # next attempt needs — deleting a corrupt object, rebuilding a sketch,
        # printing a diagnostic. Aborting discards all of it, so a repair script
        # that ends in a failed assertion erases its own repair and the next
        # attempt starts from the same broken state. Commit what was built and
        # report the failure; policy violations and validation failures still
        # roll back, and the user's undo stack still holds this step.
        if not keep_partial_on_error:
            doc.abortTransaction()
            return result(False, error, rolled_back=True)
        try:
            doc.recompute()
        except Exception:
            pass
        doc.commitTransaction()
        return result(False, error)

def undo(app) -> None:
    doc = app.ActiveDocument
    if doc is not None:
        doc.undo()
