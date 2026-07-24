import io
import traceback
import contextlib
from .types import ExecResult

def run(app, script: str) -> "ExecResult":
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
    doc.openTransaction("LLM Copilot")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(script, "<llm_script>", "exec"), g)
        doc.recompute()
        doc.commitTransaction()
        return ExecResult(True, buf.getvalue(), "")
    except Exception:
        doc.abortTransaction()
        return ExecResult(False, buf.getvalue(), traceback.format_exc())

def undo(app) -> None:
    doc = app.ActiveDocument
    if doc is not None:
        doc.undo()
