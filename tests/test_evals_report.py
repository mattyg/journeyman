import json
import os
import tempfile
import unittest

from evals import report


def _attempt(run_dir, name, checks_passed, checks_ran, score=None,
             termination="completed", issues=()):
    attempt = os.path.join(run_dir, name)
    os.makedirs(attempt)
    with open(os.path.join(attempt, "run.json"), "w") as fh:
        json.dump({
            "scenario": name.rsplit("-", 1)[0]
            if name.rsplit("-", 1)[-1].isdigit() else name,
            "kind": "create", "termination": termination,
            "checks": {"passed": checks_passed, "ran": checks_ran},
        }, fh)
    if score is not None:
        with open(os.path.join(attempt, "judge.json"), "w") as fh:
            json.dump({"score": score, "issues": list(issues)}, fh)
    return attempt


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_summary_groups_repeat_attempts(self):
        _attempt(self.dir, "cube-1", 5, 5, score=8)
        _attempt(self.dir, "cube-2", 4, 5, score=6)
        summary = report.load_summary(self.dir)
        self.assertEqual(summary["cube"]["attempts"], 2)
        self.assertEqual(summary["cube"]["mean_score"], 7.0)
        self.assertAlmostEqual(summary["cube"]["check_rate"], 0.9)

    def test_summary_without_judge_scores(self):
        _attempt(self.dir, "flange", 3, 5, termination="crashed")
        summary = report.load_summary(self.dir)
        self.assertIsNone(summary["flange"]["mean_score"])
        self.assertEqual(summary["flange"]["terminations"], ["crashed"])

    def test_benchmark_defects_listed_separately_from_harness_issues(self):
        _attempt(self.dir, "cube", 5, 5, score=6, issues=[
            {"classification": "harness-prompt", "summary": "bad gate"},
            {"classification": "benchmark-defect",
             "summary": "prompt is ambiguous about diameter"}])
        summary = report.load_summary(self.dir)
        classifications = {i["classification"]
                           for i in summary["cube"]["issues"]}
        self.assertEqual(classifications,
                         {"harness-prompt", "benchmark-defect"})
        report.print_summary(self.dir, summary)  # must not raise

    def test_issue_aggregation(self):
        _attempt(self.dir, "cube", 5, 5, score=4, issues=[
            {"classification": "harness-prompt", "summary": "bad gate"}])
        summary = report.load_summary(self.dir)
        self.assertEqual(
            summary["cube"]["issues"][0]["classification"], "harness-prompt")

    def test_diff_counts_regressions(self):
        old_dir = os.path.join(self.dir, "old")
        new_dir = os.path.join(self.dir, "new")
        os.makedirs(old_dir)
        os.makedirs(new_dir)
        _attempt(old_dir, "cube-1", 5, 5, score=8)
        _attempt(old_dir, "cube-2", 5, 5, score=8)
        _attempt(new_dir, "cube-1", 5, 5, score=3)
        _attempt(new_dir, "cube-2", 5, 5, score=3)
        regressions = report.print_diff(
            old_dir, new_dir,
            report.load_summary(old_dir), report.load_summary(new_dir))
        self.assertEqual(regressions, 1)

    def test_diff_single_attempt_small_delta_is_noise(self):
        old_dir = os.path.join(self.dir, "old")
        new_dir = os.path.join(self.dir, "new")
        os.makedirs(old_dir)
        os.makedirs(new_dir)
        _attempt(old_dir, "cube", 5, 5, score=8)
        _attempt(new_dir, "cube", 5, 5, score=7)
        regressions = report.print_diff(
            old_dir, new_dir,
            report.load_summary(old_dir), report.load_summary(new_dir))
        self.assertEqual(regressions, 0)


if __name__ == "__main__":
    unittest.main()
