"""Grading-policy tests for checks.py that need no FreeCAD.

These cover *when* a check produces a verdict rather than the geometry math:
a measurement that does not apply to the task must report ok=None (skipped),
not a failure.
"""
import unittest
from unittest import mock

from evals import checks, scenarios


def _scenario(kind="create", **expect):
    return scenarios.Scenario(
        id="s", kind=kind, prompt="p", images=[], start_document=None,
        expect=scenarios.Expectations(**expect), judge={}, path="s.json")


class _FakeDoc:
    Name = "doc"


class GroundTruthGradingTests(unittest.TestCase):
    def _run(self, scenario, model_volume, ref_volume=100.0):
        app = mock.Mock()
        app.openDocument.return_value = _FakeDoc()
        volumes = {"model": model_volume, "ref": ref_volume}
        doc = _FakeDoc()
        with mock.patch.object(
                checks, "_total_volume",
                lambda d: volumes["model"] if d is doc else volumes["ref"]):
            return checks.check_ground_truth(app, doc, scenario)

    def test_create_with_measure_is_graded(self):
        scenario = _scenario("create", ground_truth="g.FCStd", measure=True)
        self.assertTrue(self._run(scenario, 100.0)["ok"])
        self.assertFalse(self._run(scenario, 900.0)["ok"])

    def test_modify_is_measured_but_not_graded(self):
        scenario = _scenario("modify", ground_truth="g.FCStd")
        result = self._run(scenario, 830.0)
        self.assertIsNone(result["ok"])
        self.assertAlmostEqual(result["measured"], 8.3)
        self.assertIn("starting document", result["detail"])

    def test_create_without_measure_is_not_graded(self):
        scenario = _scenario("create", ground_truth="g.FCStd", measure=False)
        result = self._run(scenario, 34683.0)
        self.assertIsNone(result["ok"])
        self.assertIn("withheld exact sizes", result["detail"])

    def test_no_ground_truth_skips(self):
        result = self._run(_scenario("create"), 100.0)
        self.assertIsNone(result["ok"])


class SizeExpectationTests(unittest.TestCase):
    def test_bbox_skipped_when_measure_false(self):
        expect = scenarios.Expectations(bbox_mm=[1, 2, 3], measure=False)
        self.assertIsNone(checks.check_bbox(_FakeDoc(), expect)["ok"])

    def test_volume_skipped_when_measure_false(self):
        expect = scenarios.Expectations(volume_mm3=10.0, measure=False)
        self.assertIsNone(checks.check_volume(_FakeDoc(), expect)["ok"])


if __name__ == "__main__":
    unittest.main()
