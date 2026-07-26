import os
import tempfile
import unittest
from unittest import mock

from evals import cli


class LoadEnvTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, ".env")

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return self.path

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(cli.load_env(os.path.join(self.dir, "nope")), {})

    def test_basic_key_value(self):
        self._write("OPENROUTER_API_KEY=sk-abc123\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            loaded = cli.load_env(self.path)
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], "sk-abc123")
        self.assertEqual(loaded["OPENROUTER_API_KEY"], "sk-abc123")

    def test_real_environment_wins(self):
        self._write("OPENROUTER_API_KEY=from-file\n")
        with mock.patch.dict(os.environ,
                             {"OPENROUTER_API_KEY": "from-shell"}, clear=True):
            loaded = cli.load_env(self.path)
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], "from-shell")
            self.assertNotIn("OPENROUTER_API_KEY", loaded)

    def test_comments_blanks_and_export_prefix(self):
        self._write("# a comment\n\nexport KEY_A=one\n  KEY_B = two  \n")
        with mock.patch.dict(os.environ, {}, clear=True):
            cli.load_env(self.path)
            self.assertEqual(os.environ["KEY_A"], "one")
            self.assertEqual(os.environ["KEY_B"], "two")

    def test_quoted_values_are_unwrapped(self):
        self._write('A="dq value"\nB=\'sq value\'\n')
        with mock.patch.dict(os.environ, {}, clear=True):
            cli.load_env(self.path)
            self.assertEqual(os.environ["A"], "dq value")
            self.assertEqual(os.environ["B"], "sq value")

    def test_value_containing_equals_is_kept(self):
        self._write("URL=https://x/y?a=b\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            cli.load_env(self.path)
            self.assertEqual(os.environ["URL"], "https://x/y?a=b")

    def test_line_without_equals_ignored(self):
        self._write("JUST_A_WORD\nA=1\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            cli.load_env(self.path)
            self.assertEqual(os.environ["A"], "1")
            self.assertNotIn("JUST_A_WORD", os.environ)

    def test_empty_value_allowed(self):
        # .env.example ships keys with empty values; they must not crash.
        self._write("OPENROUTER_API_KEY=\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            cli.load_env(self.path)
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], "")


class ScriptArgsTests(unittest.TestCase):
    def test_pass_string_is_split(self):
        argv = ["prog", "script.py", "--pass", "--all --repeat 2"]
        with mock.patch.object(cli.sys, "argv", argv):
            self.assertEqual(cli.script_args(), ["--all", "--repeat", "2"])

    def test_env_args_fallback(self):
        with mock.patch.object(cli.sys, "argv", ["prog", "script.py"]), \
                mock.patch.dict(os.environ, {"EVAL_ARGS": "--scenario x"}):
            self.assertEqual(cli.script_args(), ["--scenario", "x"])

    def test_plain_argv(self):
        with mock.patch.object(cli.sys, "argv", ["prog", "--rows", "5"]), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cli.script_args(), ["--rows", "5"])


if __name__ == "__main__":
    unittest.main()
