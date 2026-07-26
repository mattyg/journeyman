import json
import os
import tempfile
import unittest

from evals import scenarios


def _write(directory, name, data):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return path


class ScenarioLoadingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_minimal_create_scenario(self):
        path = _write(self.dir, "a.json",
                      {"id": "a", "kind": "create", "prompt": "make a cube"})
        scenario = scenarios.load_scenario(path)
        self.assertEqual(scenario.id, "a")
        self.assertIsNone(scenario.start_document)
        self.assertEqual(scenario.images, [])
        self.assertIsNone(scenario.expect.bbox_mm)

    def test_relative_paths_resolve_against_benchmarks_dir(self):
        path = _write(self.dir, "b.json", {
            "id": "b", "kind": "modify", "prompt": "p",
            "start_document": "refs/x.FCStd",
            "images": ["refs/x.png"],
            "expect": {"ground_truth": "refs/x.FCStd"}})
        scenario = scenarios.load_scenario(path)
        self.assertEqual(scenario.start_document,
                         os.path.join(self.dir, "refs/x.FCStd"))
        self.assertEqual(scenario.images[0],
                         os.path.join(self.dir, "refs/x.png"))
        self.assertEqual(scenario.expect.ground_truth,
                         os.path.join(self.dir, "refs/x.FCStd"))

    def test_missing_required_field_raises(self):
        path = _write(self.dir, "c.json", {"id": "c", "kind": "create"})
        with self.assertRaises(ValueError):
            scenarios.load_scenario(path)

    def test_modify_requires_start_document(self):
        path = _write(self.dir, "d.json",
                      {"id": "d", "kind": "modify", "prompt": "p"})
        with self.assertRaises(ValueError):
            scenarios.load_scenario(path)

    def test_unknown_expect_field_raises(self):
        path = _write(self.dir, "e.json", {
            "id": "e", "kind": "create", "prompt": "p",
            "expect": {"bogus": 1}})
        with self.assertRaises(ValueError):
            scenarios.load_scenario(path)

    def test_duplicate_ids_rejected(self):
        _write(self.dir, "f.json",
               {"id": "same", "kind": "create", "prompt": "p"})
        _write(self.dir, "g.json",
               {"id": "same", "kind": "create", "prompt": "p"})
        with self.assertRaises(ValueError):
            scenarios.load_all(self.dir)

    def test_repo_benchmarks_all_load(self):
        loaded = scenarios.load_all()
        self.assertTrue(any(s.id == "hanger-modify" for s in loaded))


if __name__ == "__main__":
    unittest.main()
