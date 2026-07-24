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


def assert_feature(obj, solids=None):
    """Raise a specific error unless ``obj`` built into usable geometry.

    Hand-rolled checks (``pad.Shape.isValid()``) raise an opaque OCC error when
    a feature fails, or silently pass on a null shape — either way the script
    reports the wrong thing about which step broke. This names the failure.

    Pass ``solids`` to also assert an exact solid count.
    """
    name = getattr(obj, "Name", None) or repr(obj)
    if obj is None:
        raise ValueError("assert_feature: no object (the feature was not created)")
    errors = getattr(obj, "State", None)
    if errors and "Invalid" in errors:
        raise ValueError(f"{name} is in an Invalid state: {errors}")
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


def assert_sketch_constrained(sketch):
    """Raise unless ``sketch`` is closed and fully constrained."""
    name = getattr(sketch, "Name", None) or repr(sketch)
    shape = getattr(sketch, "Shape", None)
    if shape is None or shape.isNull():
        raise ValueError(f"{name} has no shape")
    if shape.Wires and not all(wire.isClosed() for wire in shape.Wires):
        raise ValueError(
            f"{name} is not a closed wire; a pad or pocket profile must close")
    dof = sketch.solve() if hasattr(sketch, "solve") else 0
    remaining = getattr(sketch, "FullyConstrained", None)
    if remaining is False:
        raise ValueError(
            f"{name} is not fully constrained (solver status {dof})")
    return sketch


def run(app, script: str, validate=False, rollback_on_failure=False) -> "ExecResult":
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
    doc.openTransaction("LLM Copilot")
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
                    return result(
                        False, "POST_EXECUTION_VALIDATION_FAILED",
                        False, validation, True)
        doc.commitTransaction()
        return result(True, validation_ok=validation_ok, validation=validation)
    except Exception:
        doc.abortTransaction()
        return result(
            False, _annotate_traceback(script, traceback.format_exc()))

def undo(app) -> None:
    doc = app.ActiveDocument
    if doc is not None:
        doc.undo()
