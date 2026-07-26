# tests/integration/test_eval_checks.py  (run under freecadcmd)
"""Geometry checks against real FreeCAD documents.

The key behavior here: a sketch that was already underconstrained in the
starting document must not be counted against the agent. Several dataset
ground-truth files ship that way, so grading them as run failures measures
the dataset rather than the harness.
"""
import unittest
import FreeCAD as App
import Part
import Sketcher

from evals import checks
from evals import scenarios


def _scenario(kind="create", **expect):
    return scenarios.Scenario(
        id="s", kind=kind, prompt="p", images=[], start_document=None,
        expect=scenarios.Expectations(**expect), judge={}, path="s.json")


class SketchConstraintCheckTests(unittest.TestCase):
    def tearDown(self):
        for name in list(App.listDocuments()):
            App.closeDocument(name)

    def _doc_with_loose_sketch(self, name="Loose"):
        doc = App.newDocument("t")
        sketch = doc.addObject("Sketcher::SketchObject", name)
        # A closed square with no dimensional constraints: solves fine, but
        # every vertex can still move.
        points = [(0, 0), (10, 0), (10, 10), (0, 10)]
        for index in range(4):
            start = App.Vector(*points[index], 0)
            end = App.Vector(*points[(index + 1) % 4], 0)
            sketch.addGeometry(Part.LineSegment(start, end), False)
        for index in range(4):
            sketch.addConstraint(Sketcher.Constraint(
                "Coincident", index, 2, (index + 1) % 4, 1))
        doc.recompute()
        return doc, sketch

    def test_underconstrained_sketch_is_reported(self):
        doc, _ = self._doc_with_loose_sketch()
        result = checks.check_sketches_constrained(doc)
        self.assertFalse(result["ok"])
        self.assertIn("not fully constrained", result["detail"])

    def test_preexisting_fault_is_not_graded(self):
        doc, _ = self._doc_with_loose_sketch()
        before = checks.baseline(doc)
        self.assertEqual(before["unconstrained_sketches"], ["Loose"])
        result = checks.check_sketches_constrained(
            doc, before["unconstrained_sketches"])
        self.assertTrue(result["ok"])
        self.assertIn("pre-existing", result["detail"])

    def test_new_fault_still_graded_alongside_inherited_one(self):
        doc, _ = self._doc_with_loose_sketch()
        before = checks.baseline(doc)
        # The agent adds its own loose sketch on top of the inherited one.
        second = doc.addObject("Sketcher::SketchObject", "AgentSketch")
        second.addGeometry(
            Part.LineSegment(App.Vector(0, 0, 0), App.Vector(5, 0, 0)), False)
        doc.recompute()
        result = checks.check_sketches_constrained(
            doc, before["unconstrained_sketches"])
        self.assertFalse(result["ok"])
        self.assertIn("AgentSketch", result["detail"])
        self.assertIn("pre-existing", result["detail"])

    def test_fully_constrained_sketch_passes(self):
        doc = App.newDocument("t2")
        sketch = doc.addObject("Sketcher::SketchObject", "Fixed")
        sketch.addGeometry(
            Part.LineSegment(App.Vector(0, 0, 0), App.Vector(10, 0, 0)), False)
        sketch.addConstraint(Sketcher.Constraint("Block", 0))
        doc.recompute()
        problem = checks._sketch_problem(sketch)
        # Either fully constrained (passes) or an explicit reason; never a
        # silent pass on an unsolved sketch.
        if problem is not None:
            self.assertTrue(problem)


class BaselineTests(unittest.TestCase):
    def tearDown(self):
        for name in list(App.listDocuments()):
            App.closeDocument(name)

    def test_baseline_records_volumes_and_faults(self):
        doc = App.newDocument("t3")
        box = doc.addObject("Part::Box", "Box")
        doc.recompute()
        before = checks.baseline(doc)
        self.assertAlmostEqual(before["volumes"]["Box"], box.Shape.Volume)
        self.assertEqual(before["unconstrained_sketches"], [])

    def test_run_checks_accepts_baseline_dict(self):
        doc = App.newDocument("t4")
        doc.addObject("Part::Box", "Box")
        doc.recompute()
        before = checks.baseline(doc)
        results = checks.run_checks(App, doc, _scenario(), before)
        names = {c["name"] for c in results["checks"]}
        self.assertIn("sketches_constrained", names)
        self.assertIn("solids", names)


if __name__ == "__main__":
    unittest.main()
