import unittest
from unittest import mock

from evals import synthesize

PARAMS_TEXT = """- outer_diameter = 107.156mm
- flange_thickness = 21.034mm
- bolt_circle_diameter = 87.312mm
- bolt_hole_diameter = 9.922mm
- number_bolt_holes = 4
- bore_diameter = 49.213mm"""

META = {
    "dataset_id": "abc123",
    "slug": "flange",
    "name": "Round mounting flange",
    "description": "A round mounting flange.",
    "key_parameters": PARAMS_TEXT,
    "image": "refs/flange.png",
    "fcstd": "refs/flange.FCStd",
}

WORDINGS = {
    "partial_spec": "please model a flange, 107mm across",
    "vague": "need a flange, roughly 100mm",
    "functional": "I need to mount a motor to a plate",
    "modify_param": "open the bolt holes up a bit",
    "modify_underspec": "make it beefier",
    "modify_image": "see the holes in the picture? open those up to 12mm",
}


class ParameterParsingTests(unittest.TestCase):
    def test_parses_dash_list(self):
        params = synthesize.parse_parameters(PARAMS_TEXT)
        self.assertEqual(len(params), 6)
        self.assertEqual(params[0]["name"], "outer_diameter")
        self.assertEqual(params[0]["value"], 107.156)
        self.assertEqual(params[0]["unit"], "mm")
        self.assertEqual(params[4]["unit"], "")  # bare count

    def test_ignores_prose_lines(self):
        params = synthesize.parse_parameters(
            "The flange is round.\n- width = 5mm\nSee image.")
        self.assertEqual([p["name"] for p in params], ["width"])

    def test_empty_text(self):
        self.assertEqual(synthesize.parse_parameters(""), [])
        self.assertEqual(synthesize.parse_parameters(None), [])

    def test_comma_separated_single_line(self):
        # Some dataset rows put every parameter on one comma-separated line.
        params = synthesize.parse_parameters(
            "- cup_diameter=80.0mm, cup_height=120.0mm, cup_thickness=2.0mm")
        self.assertEqual([p["name"] for p in params],
                         ["cup_diameter", "cup_height", "cup_thickness"])
        self.assertEqual(params[1]["value"], 120.0)
        self.assertEqual(params[2]["unit"], "mm")

    def test_duplicate_names_kept_once(self):
        params = synthesize.parse_parameters("- a=1mm, a=2mm\n- b=3mm")
        self.assertEqual([p["name"] for p in params], ["a", "b"])
        self.assertEqual(params[0]["value"], 1.0)


class WithholdingTests(unittest.TestCase):
    def test_deterministic_for_same_seed(self):
        params = synthesize.parse_parameters(PARAMS_TEXT)
        kept_a, withheld_a = synthesize.split_withheld(params, "seed-1")
        kept_b, withheld_b = synthesize.split_withheld(params, "seed-1")
        self.assertEqual([p["name"] for p in withheld_a],
                         [p["name"] for p in withheld_b])

    def test_withholds_some_keeps_some(self):
        params = synthesize.parse_parameters(PARAMS_TEXT)
        kept, withheld = synthesize.split_withheld(params, "seed")
        self.assertTrue(kept)
        self.assertTrue(withheld)
        self.assertEqual(len(kept) + len(withheld), len(params))

    def test_single_param_never_withheld(self):
        params = [{"name": "d", "value": 5.0, "unit": "mm"}]
        kept, withheld = synthesize.split_withheld(params, "seed")
        self.assertEqual(kept, params)
        self.assertEqual(withheld, [])


class ModifyPickTests(unittest.TestCase):
    def test_deterministic_and_scaled(self):
        params = synthesize.parse_parameters(PARAMS_TEXT)
        pick_a = synthesize.pick_modify_param(params, "abc123")
        pick_b = synthesize.pick_modify_param(params, "abc123")
        self.assertEqual(pick_a["param"]["name"], pick_b["param"]["name"])
        self.assertAlmostEqual(
            pick_a["new_value"], round(pick_a["param"]["value"] * 1.25, 2))

    def test_no_params(self):
        self.assertIsNone(synthesize.pick_modify_param([], "x"))


class ScenarioShapeTests(unittest.TestCase):
    def test_mechanical_scenarios(self):
        scenarios = synthesize.mechanical_scenarios(META)
        ids = [s["id"] for s in scenarios]
        self.assertEqual(ids, ["flange-create-img", "flange-image-minimal",
                               "flange-create-text"])
        minimal = scenarios[1]
        self.assertFalse(minimal["expect"]["measure"])
        self.assertNotIn("107", minimal["prompt"])

    def test_mechanical_without_image(self):
        meta = dict(META, image=None)
        ids = [s["id"] for s in synthesize.mechanical_scenarios(meta)]
        self.assertEqual(ids, ["flange-create-text"])

    def test_llm_scenarios(self):
        params = synthesize.parse_parameters(PARAMS_TEXT)
        kept, withheld = synthesize.split_withheld(params, META["dataset_id"])
        modify = synthesize.pick_modify_param(params, META["dataset_id"])
        scenarios = synthesize.llm_scenarios(
            META, params, kept, withheld, modify, WORDINGS)
        by_id = {s["id"]: s for s in scenarios}
        self.assertEqual(set(by_id), {
            "flange-partial-spec", "flange-vague", "flange-functional",
            "flange-modify-param", "flange-modify-underspec",
            "flange-modify-img"})
        self.assertEqual(by_id["flange-partial-spec"]["judge"]["withheld"],
                         [synthesize._fmt(p) for p in withheld])
        self.assertFalse(by_id["flange-vague"]["expect"]["measure"])
        target = by_id["flange-modify-param"]["judge"]["target"]
        self.assertEqual(target["parameter"], modify["param"]["name"])
        for scenario in scenarios:
            if scenario["kind"] == "modify":
                self.assertEqual(scenario["start_document"], META["fcstd"])

    def test_llm_scenarios_without_fcstd_skips_modify(self):
        meta = dict(META, fcstd=None)
        scenarios = synthesize.llm_scenarios(
            meta, [], [], [], None, WORDINGS)
        self.assertEqual(len(scenarios), 3)

    def test_modify_variants_cover_image_and_text_only(self):
        params = synthesize.parse_parameters(PARAMS_TEXT)
        modify = synthesize.pick_modify_param(params, META["dataset_id"])
        scenarios = synthesize.llm_scenarios(
            META, params, params, [], modify, WORDINGS)
        by_id = {s["id"]: s for s in scenarios}
        text_only = by_id["flange-modify-param"]
        with_image = by_id["flange-modify-img"]
        # both start from the same existing model
        self.assertEqual(text_only["start_document"], META["fcstd"])
        self.assertEqual(with_image["start_document"], META["fcstd"])
        # the text-only one carries no attachment; the image one does
        self.assertFalse(text_only.get("images"))
        self.assertEqual(with_image["images"], [META["image"]])
        self.assertEqual(with_image["judge"]["variant"], "modify-img")

    def test_modify_img_skipped_without_image(self):
        meta = dict(META, image=None)
        params = synthesize.parse_parameters(PARAMS_TEXT)
        scenarios = synthesize.llm_scenarios(
            meta, params, params, [], None, WORDINGS)
        self.assertNotIn("flange-modify-img", {s["id"] for s in scenarios})

    def test_modify_img_judge_warns_image_is_not_the_target(self):
        params = synthesize.parse_parameters(PARAMS_TEXT)
        scenarios = synthesize.llm_scenarios(
            META, params, params, [], None, WORDINGS)
        notes = next(s for s in scenarios
                     if s["id"] == "flange-modify-img")["judge"]["notes"]
        self.assertIn("NOT the desired result", notes)


class GeneratorPlumbingTests(unittest.TestCase):
    def test_extract_json_with_fences(self):
        data = synthesize._extract_json('junk\n```json\n{"a": 1}\n```')
        self.assertEqual(data, {"a": 1})

    def test_generate_wordings_validates_fields(self):
        incomplete = dict(WORDINGS)
        del incomplete["vague"]
        with mock.patch.object(synthesize.judge, "_raw_complete",
                               return_value=str(incomplete).replace("'", '"')):
            with self.assertRaises(ValueError):
                synthesize.generate_wordings(META, [], [], [], None, None)

    def test_generate_wordings_passes_withheld_names_only(self):
        params = synthesize.parse_parameters(PARAMS_TEXT)
        kept, withheld = synthesize.split_withheld(params, "s")
        captured = {}

        def fake(settings, blocks):
            captured["prompt"] = blocks[0]["text"]
            import json
            return json.dumps(WORDINGS)

        with mock.patch.object(synthesize.judge, "_raw_complete", fake):
            synthesize.generate_wordings(META, params, kept, withheld,
                                         None, None)
        for param in withheld:
            # the withheld list must name parameters without their values
            self.assertNotIn(synthesize._fmt(param), captured[
                "prompt"].split("withheld parameters:")[1].split('"')[0])


if __name__ == "__main__":
    unittest.main()
