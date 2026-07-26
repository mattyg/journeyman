import unittest

from evals import judge


class VerdictParsingTests(unittest.TestCase):
    def test_plain_json(self):
        verdict = judge._parse_verdict(
            '{"score": 7, "verdict": "ok", "issues": []}')
        self.assertEqual(verdict["score"], 7)

    def test_fenced_json_with_prose(self):
        text = ('Here is my assessment.\n```json\n'
                '{"score": 4, "issues": [{"classification": "harness-prompt",'
                ' "summary": "s", "evidence": "e"}]}\n```')
        verdict = judge._parse_verdict(text)
        self.assertEqual(verdict["score"], 4)
        self.assertEqual(len(verdict["issues"]), 1)

    def test_score_clamped(self):
        self.assertEqual(judge._parse_verdict('{"score": 99}')["score"], 10)
        self.assertEqual(judge._parse_verdict('{"score": -3}')["score"], 0)

    def test_missing_json_raises(self):
        with self.assertRaises(ValueError):
            judge._parse_verdict("no json here")

    def test_missing_issues_defaulted(self):
        self.assertEqual(judge._parse_verdict('{"score": 5}')["issues"], [])


class RubricSelectionTests(unittest.TestCase):
    def test_all_kind_combinations_have_rubrics(self):
        for kind in ("create", "modify"):
            for had_image in (True, False):
                self.assertIn((kind, had_image), judge._RUBRIC_BY_KIND)

    def test_modify_with_image_rubric_states_image_is_current_state(self):
        rubric = judge._RUBRIC_BY_KIND[("modify", True)]
        self.assertIn("not the desired end state", rubric)
        self.assertIn("failure", rubric)

    def test_text_only_create_rubric_forbids_visual_penalty(self):
        self.assertIn("NEVER penalize",
                      judge._RUBRIC_BY_KIND[("create", False)])


class AmbiguityPolicyTests(unittest.TestCase):
    def test_common_rubric_covers_ambiguous_instructions(self):
        rubric = judge._RUBRIC_COMMON
        self.assertIn("Ambiguous instructions", rubric)
        self.assertIn("benchmark-defect", rubric)
        # A recorded numeric target must not be treated as the answer key.
        self.assertIn("only correct answer", rubric)

    def test_benchmark_defect_is_a_documented_classification(self):
        self.assertIn("benchmark-defect: the task prompt",
                      judge._RUBRIC_COMMON)


if __name__ == "__main__":
    unittest.main()
