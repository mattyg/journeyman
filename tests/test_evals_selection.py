import json
import os
import tempfile
import unittest
from unittest import mock

from evals import scenarios


def _make(directory, scenario_id, variant, kind="create"):
    data = {"id": scenario_id, "kind": kind, "prompt": "p",
            "judge": {"variant": variant}}
    if kind == "modify":
        data["start_document"] = "refs/x.FCStd"
    with open(os.path.join(directory, scenario_id + ".json"), "w") as fh:
        json.dump(data, fh)


def _select(argv, directory):
    """Run runner.main's selection logic without touching FreeCAD or the LLM."""
    from evals import runner
    captured = {}

    def fake_run_scenario(app, scenario, settings, out_dir, budget):
        captured.setdefault("ids", []).append(scenario.id)
        return {"termination": "completed",
                "checks": {"all_ok": True, "passed": 1, "ran": 1}}

    fake_settings = mock.Mock(model="test/model")
    # load_all binds BENCHMARKS_DIR as a default argument, so redirect the
    # function itself. Capture the real one first: runner.scenarios is this
    # same module object, so calling through it after patching would recurse.
    real_load_all = scenarios.load_all
    with mock.patch.object(runner.scenarios, "load_all",
                           lambda directory=directory:
                           real_load_all(directory)), \
            mock.patch.object(runner, "run_scenario", fake_run_scenario), \
            mock.patch.object(runner, "build_settings",
                              return_value=fake_settings), \
            mock.patch.dict("sys.modules", {"FreeCAD": mock.Mock()}):
        runner.main(argv)
    return captured.get("ids", [])


class PrefixSelectionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for variant in ("create-img", "create-text", "vague",
                        "modify-param", "modify-img"):
            kind = "modify" if variant.startswith("modify") else "create"
            _make(self.dir, f"disc-spring-{variant}", variant, kind)
        _make(self.dir, "water-cup-create-img", "create-img")
        _make(self.dir, "water-cup-modify-img", "modify-img", "modify")

    def test_prefix_selects_all_variants_of_one_part(self):
        ids = _select(["--prefix", "disc-spring"], self.dir)
        self.assertEqual(len(ids), 5)
        self.assertTrue(all(i.startswith("disc-spring") for i in ids))

    def test_prefix_is_sorted_and_excludes_other_parts(self):
        ids = _select(["--prefix", "disc-spring"], self.dir)
        self.assertEqual(ids, sorted(ids))
        self.assertNotIn("water-cup-create-img", ids)

    def test_prefix_repeatable(self):
        ids = _select(["--prefix", "disc-spring", "--prefix", "water-cup"],
                      self.dir)
        self.assertEqual(len(ids), 7)

    def test_prefix_and_scenario_combine_without_duplicates(self):
        ids = _select(["--prefix", "disc-spring",
                       "--scenario", "disc-spring-vague"], self.dir)
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)

    def test_variant_filter(self):
        ids = _select(["--all", "--variant", "modify-img"], self.dir)
        self.assertEqual(sorted(ids),
                         ["disc-spring-modify-img", "water-cup-modify-img"])

    def test_prefix_with_variant_filter(self):
        ids = _select(["--prefix", "disc-spring", "--variant", "create-img"],
                      self.dir)
        self.assertEqual(ids, ["disc-spring-create-img"])

    def test_repeat_multiplies_attempts(self):
        ids = _select(["--prefix", "water-cup", "--repeat", "2"], self.dir)
        self.assertEqual(len(ids), 4)

    def test_unknown_prefix_exits(self):
        with self.assertRaises(SystemExit):
            _select(["--prefix", "no-such-part"], self.dir)

    def test_unmatched_variant_exits(self):
        with self.assertRaises(SystemExit):
            _select(["--prefix", "water-cup", "--variant", "vague"], self.dir)

    def test_no_selector_exits(self):
        with self.assertRaises(SystemExit):
            _select([], self.dir)


if __name__ == "__main__":
    unittest.main()
