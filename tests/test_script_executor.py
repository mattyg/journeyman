# tests/test_script_executor.py
import pytest

from freecad.llm_copilot import script_executor
from freecad.llm_copilot.script_executor import (
    _annotate_traceback, assert_feature, assert_sketch_constrained)

SCRIPT = "\n".join(f"line{n} = {n}" for n in range(1, 21))


def test_annotates_script_frame_with_marked_source_line():
    text = (
        "Traceback (most recent call last):\n"
        '  File "/mod/script_executor.py", line 84, in run\n'
        "    exec(compile(script, ...))\n"
        '  File "<llm_script>", line 13, in <module>\n'
        "Part.OCCError: NULL shape\n")
    out = _annotate_traceback(SCRIPT, text)
    assert ">>> 13 | line13 = 13" in out
    assert "    11 | line11 = 11" in out
    assert "    15 | line15 = 15" in out
    # Context only: nothing outside the +/-2 window.
    assert "line10" not in out and "line16" not in out
    # The host frame is untouched — only <llm_script> frames get source.
    assert "script_executor.py" in out
    assert out.count(">>>") == 1


def test_annotation_preserves_original_traceback_text():
    text = '  File "<llm_script>", line 2, in <module>\nValueError: boom\n'
    out = _annotate_traceback(SCRIPT, text)
    for original in text.strip().splitlines():
        assert original in out
    assert out.endswith("\n")


def test_clamps_context_at_script_boundaries():
    out = _annotate_traceback(SCRIPT, '  File "<llm_script>", line 1\n')
    assert ">>> 1 | line1 = 1" in out
    assert "line4" not in out  # clamped at the start, no negative lines
    out = _annotate_traceback(SCRIPT, '  File "<llm_script>", line 20\n')
    assert ">>> 20 | line20 = 20" in out


def test_out_of_range_line_is_left_alone():
    text = '  File "<llm_script>", line 99, in <module>\n'
    assert _annotate_traceback(SCRIPT, text) == text


def test_multiple_script_frames_each_annotated():
    text = (
        '  File "<llm_script>", line 3, in <module>\n'
        '  File "<llm_script>", line 7, in helper\n')
    out = _annotate_traceback(SCRIPT, text)
    assert ">>> 3 | line3 = 3" in out
    assert ">>> 7 | line7 = 7" in out
    assert out.count(">>>") == 2


def test_traceback_without_script_frames_is_unchanged():
    text = "Traceback:\n  File \"/other.py\", line 3, in f\nKeyError: 'x'\n"
    assert _annotate_traceback(SCRIPT, text) == text


# --- assert_feature / assert_sketch_constrained ---


class FakeShape:
    def __init__(self, null=False, valid=True, solids=1, wires=()):
        self._null, self._valid = null, valid
        self.Solids = [object()] * solids
        self.Wires = list(wires)

    def isNull(self): return self._null
    def isValid(self): return self._valid


class FakeObj:
    def __init__(self, name="PlatePad", shape=None, state=""):
        self.Name, self.Shape, self.State = name, shape, state


class FakeWire:
    def __init__(self, closed): self._closed = closed
    def isClosed(self): return self._closed


def test_null_shape_names_the_feature_and_the_likely_cause():
    """The transcript's failure: a pad that computed to a NULL shape."""
    with pytest.raises(ValueError) as excinfo:
        assert_feature(FakeObj(shape=FakeShape(null=True)))
    message = str(excinfo.value)
    assert "PlatePad" in message and "NULL shape" in message
    assert "closed wire" in message


def test_valid_feature_passes_through():
    obj = FakeObj(shape=FakeShape())
    assert assert_feature(obj) is obj


def test_solid_count_is_checked_when_requested():
    with pytest.raises(ValueError, match="produced 1 solids, expected 2"):
        assert_feature(FakeObj(shape=FakeShape(solids=1)), solids=2)
    assert_feature(FakeObj(shape=FakeShape(solids=2)), solids=2)


def test_zero_solids_fails_by_default():
    with pytest.raises(ValueError, match="no solids"):
        assert_feature(FakeObj(shape=FakeShape(solids=0)))


def test_invalid_state_is_reported_before_the_shape():
    with pytest.raises(ValueError, match="Invalid state"):
        assert_feature(FakeObj(shape=FakeShape(), state="Invalid"))


def test_missing_object_is_reported():
    with pytest.raises(ValueError, match="was not created"):
        assert_feature(None)


def test_open_sketch_wire_is_rejected():
    sketch = FakeObj("PlateSketch", FakeShape(wires=[FakeWire(False)]))
    with pytest.raises(ValueError, match="not a closed wire"):
        assert_sketch_constrained(sketch)


def test_closed_and_constrained_sketch_passes():
    sketch = FakeObj("PlateSketch", FakeShape(wires=[FakeWire(True)]))
    sketch.FullyConstrained = True
    assert assert_sketch_constrained(sketch) is sketch


def test_underconstrained_sketch_is_rejected():
    sketch = FakeObj("PlateSketch", FakeShape(wires=[FakeWire(True)]))
    sketch.FullyConstrained = False
    sketch.solve = lambda: 3
    with pytest.raises(ValueError, match="not fully constrained"):
        assert_sketch_constrained(sketch)


def test_empty_sketch_reports_missing_geometry_not_missing_shape():
    """The terminal error of climbing-hanger-transcript-3.

    The sketch had geoms: 0 and was reported as "has no shape", pointing the
    model at shape construction when the real problem was an empty sketch.
    """
    sketch = FakeObj("Sketch_OuterProfile", FakeShape(null=True))
    sketch.Geometry = []
    with pytest.raises(ValueError, match="has no geometry"):
        assert_sketch_constrained(sketch)


def test_sketch_with_geometry_but_no_shape_says_so():
    sketch = FakeObj("Sketch_OuterProfile", FakeShape(null=True))
    sketch.Geometry = [object(), object()]
    with pytest.raises(ValueError) as excinfo:
        assert_sketch_constrained(sketch)
    message = str(excinfo.value)
    assert "2 geometry elements" in message
    assert "failed to build" in message


class FakeDoc:
    def __init__(self, obj, clears=True):
        self._obj, self._clears = obj, clears
        self.recomputes = 0

    def recompute(self):
        self.recomputes += 1
        if self._clears:
            self._obj.State = []


def test_touched_alone_recomputes_instead_of_failing():
    """'Touched' means the feature awaits a recompute, not that it is broken."""
    obj = FakeObj(shape=FakeShape(), state=["Touched"])
    obj.Document = FakeDoc(obj)
    assert assert_feature(obj) is obj
    assert obj.Document.recomputes == 1


def test_touched_that_survives_recompute_is_not_an_error():
    obj = FakeObj(shape=FakeShape(), state=["Touched"])
    obj.Document = FakeDoc(obj, clears=False)
    assert assert_feature(obj) is obj


def test_invalid_state_still_raises_without_recomputing():
    obj = FakeObj(shape=FakeShape(), state=["Touched", "Invalid"])
    obj.Document = FakeDoc(obj)
    with pytest.raises(ValueError, match="Invalid state"):
        assert_feature(obj)
    assert obj.Document.recomputes == 0


# --- partial work survives a raising script ---

class RecordingDoc:
    """A document that records transaction outcomes, as FreeCAD would."""

    UndoMode = 1

    def __init__(self):
        self.objects = ["Sketch_OuterProfile"]
        self.committed = self.aborted = self.recomputes = 0

    def openTransaction(self, _name): pass
    def commitTransaction(self): self.committed += 1
    def abortTransaction(self):
        self.aborted += 1
        self.objects = ["Sketch_OuterProfile"]  # abort restores the deletion
    def recompute(self): self.recomputes += 1
    def removeObject(self, name):
        if name in self.objects:
            self.objects.remove(name)


class RecordingApp:
    def __init__(self):
        self.ActiveDocument = RecordingDoc()
        self.Console = type("C", (), {})()


REPAIR_THEN_FAIL = (
    "doc = App.ActiveDocument\n"
    "doc.removeObject('Sketch_OuterProfile')\n"
    "print('cleaned')\n"
    "raise ValueError('Sketch_OuterProfile has no geometry')\n")


def test_cleanup_survives_a_script_that_raises_afterwards():
    """climbing-hanger-transcript-3: repair scripts erased their own repair."""
    app = RecordingApp()
    result = script_executor.run(app, REPAIR_THEN_FAIL)
    assert result.ok is False
    assert "has no geometry" in result.error
    assert app.ActiveDocument.objects == [], "the deletion was rolled back"
    assert app.ActiveDocument.committed == 1
    assert app.ActiveDocument.aborted == 0
    assert result.rolled_back is False
    # Output written before the failure still reaches the model.
    assert "cleaned" in result.output


def test_opting_out_restores_the_old_rollback_behaviour():
    app = RecordingApp()
    result = script_executor.run(
        app, REPAIR_THEN_FAIL, keep_partial_on_error=False)
    assert result.ok is False
    assert app.ActiveDocument.objects == ["Sketch_OuterProfile"]
    assert app.ActiveDocument.aborted == 1
    assert result.rolled_back is True


def test_a_script_that_changes_nothing_before_raising_leaves_no_trace():
    app = RecordingApp()
    result = script_executor.run(app, "raise ValueError('boom')\n")
    assert result.ok is False
    assert app.ActiveDocument.objects == ["Sketch_OuterProfile"]


def test_successful_script_still_commits_once():
    app = RecordingApp()
    result = script_executor.run(app, "print('ok')\n")
    assert result.ok is True
    assert app.ActiveDocument.committed == 1
    assert app.ActiveDocument.aborted == 0
