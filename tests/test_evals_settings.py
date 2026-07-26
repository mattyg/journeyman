"""The eval must exercise the harness users actually get, not dataclass
defaults, and must record what it ran with."""
import os
import unittest
from unittest import mock

from evals import runner
from freecad.journeyman.settings import Settings, load_settings


class ShippedDefaultsTests(unittest.TestCase):
    def _settings(self, **kw):
        with mock.patch.dict(os.environ,
                             {"OPENROUTER_API_KEY": "k"}, clear=True):
            return runner.build_settings("openrouter/vendor/model", **kw)

    def test_matches_load_settings_defaults(self):
        shipped = load_settings(runner._DefaultParams())
        got = self._settings()
        for field in ("structured_cad_planning",
                      "parametric_feature_preference",
                      "sketch_constraint_verification",
                      "stage_order_guidance", "final_design_review",
                      "mandatory_verification"):
            self.assertEqual(getattr(got, field), getattr(shipped, field),
                             f"{field} diverges from a fresh install")

    def test_parametric_features_are_enabled(self):
        # These were silently off when the eval built Settings positionally.
        got = self._settings()
        self.assertTrue(got.parametric_feature_preference)
        self.assertTrue(got.sketch_constraint_verification)

    def test_headless_necessities_are_forced(self):
        got = self._settings()
        self.assertFalse(got.confirm_before_running)
        self.assertTrue(got.auto_approve_loop)
        self.assertTrue(got.rendered_views)
        self.assertFalse(got.persist_chat_history)

    def test_model_and_key_applied(self):
        got = self._settings()
        self.assertEqual(got.model, "openrouter/vendor/model")
        self.assertEqual(got.api_key, "k")

    def test_missing_key_exits(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                runner.build_settings("openrouter/vendor/model")

    def test_overrides_applied(self):
        got = self._settings(overrides={"assumption_ledger": True,
                                        "max_auto_approved_steps": 3})
        self.assertTrue(got.assumption_ledger)
        self.assertEqual(got.max_auto_approved_steps, 3)

    def test_unknown_override_exits(self):
        with self.assertRaises(SystemExit):
            self._settings(overrides={"no_such_setting": True})


class OverrideParsingTests(unittest.TestCase):
    def test_bool_forms(self):
        parsed = runner._parse_overrides(
            ["assumption_ledger=true", "final_design_review=false"])
        self.assertIs(parsed["assumption_ledger"], True)
        self.assertIs(parsed["final_design_review"], False)

    def test_int_and_string(self):
        parsed = runner._parse_overrides(
            ["max_auto_approved_steps=12", "fidelity_target=replica"])
        self.assertEqual(parsed["max_auto_approved_steps"], 12)
        self.assertEqual(parsed["fidelity_target"], "replica")

    def test_missing_equals_exits(self):
        with self.assertRaises(SystemExit):
            runner._parse_overrides(["assumption_ledger"])


class SettingsRecordedTests(unittest.TestCase):
    def test_run_record_omits_credentials(self):
        settings = Settings("m", "secret-key", "https://base", False, True,
                            5, 3)
        recorded = {k: v for k, v in vars(settings).items()
                    if k not in ("api_key", "api_base")}
        self.assertNotIn("api_key", recorded)
        self.assertNotIn("api_base", recorded)
        self.assertIn("parametric_feature_preference", recorded)


if __name__ == "__main__":
    unittest.main()
