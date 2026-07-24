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


def run(app, script: str, validate=False, rollback_on_failure=False) -> "ExecResult":
    doc = app.ActiveDocument
    if doc is None:
        return ExecResult(False, "", "NO_ACTIVE_DOCUMENT")
    g = {"App": app, "FreeCAD": app}
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
