# tests/test_transcript_export.py
from freecad.journeyman.transcript_export import entries_to_markdown
from freecad.journeyman.types import ExecResult


def test_entries_to_markdown_renders_all_kinds():
    entries = [
        {"kind": "user", "text": "make a box",
         "images": [{"name": "ref.png", "data": "QQ=="}]},
        {"kind": "reasoning", "text": "plan first"},
        {"kind": "tool", "tool": "run_freecad_script",
         "summary": "add a box", "details": "import Part",
         "result": "Not executed — declined by user."},
        {"kind": "step", "intent": "add a box", "script": "import Part",
         "result": ExecResult(
             False, "some output", "boom", validation="bad",
             stderr="trace", console_warnings="warn",
             console_errors="err", rolled_back=True)},
        {"kind": "tool", "tool": "inspect_document",
         "summary": "q", "details": "q", "result": "inspection data"},
        {"kind": "question", "question": "which?", "answer": ["a"],
         "options": [{"id": "a", "label": "A", "description": "first"},
                     {"id": "b", "label": "B", "description": "second"}]},
        {"kind": "timeout", "message": "slow", "decision": False},
        {"kind": "context", "messages": [{"role": "user", "content": "x"}]},
        {"kind": "text", "html": "<b>Done</b><br>all set"},
        {"kind": "status", "text": "Working…"},
    ]
    md = entries_to_markdown(entries)
    assert "## You" in md and "make a box" in md
    assert "_Attached: ref.png_" in md
    assert "> **Thinking**" in md and "> plan first" in md
    assert "### Tool · run_freecad_script — add a box" in md
    assert "Not executed — declined by user." in md
    assert "### add a box" in md and "```python\nimport Part\n```" in md
    assert "**Result:** Failed" in md
    assert "**Error:**" in md and "boom" in md
    assert "**Output:**" in md and "some output" in md
    assert "**Validation:**" in md and "bad" in md
    assert "**Standard error:**" in md and "trace" in md
    assert "**FreeCAD console warnings:**" in md and "warn" in md
    assert "**FreeCAD console errors:**" in md and "err" in md
    assert "**Rolled back to the previous state.**" in md
    assert "inspection data" in md
    assert "### Question" in md and "`a` A — first" in md
    assert "**Selected:** a" in md
    assert "### Model request timed out" in md and "**Decision:** Stopped" in md
    assert "_Context sent to the model (1 messages)._" in md
    assert "Done\nall set" in md
    assert "Working…" not in md


def test_entries_to_markdown_empty():
    assert entries_to_markdown([]) == ""
    assert entries_to_markdown([{"kind": "status", "text": "x"}]) == ""
