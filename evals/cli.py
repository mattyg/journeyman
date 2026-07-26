"""Argument and environment plumbing for the eval scripts.

freecadcmd parses the command line itself and rejects unknown options, so
script arguments must be smuggled through as one quoted string:

    freecadcmd evals/runner.py --pass "--scenario hanger-modify"

or via the EVAL_ARGS environment variable:

    EVAL_ARGS="--all --repeat 2" freecadcmd evals/runner.py

Under plain python3 the normal argv is used unchanged.

API keys and model overrides can live in a gitignored .env at the repo root
(see .env.example); load_env() reads it without overriding anything already
set in the real environment.
"""
import os
import shlex
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")


def load_env(path=ENV_PATH):
    """Load KEY=value lines from .env into os.environ.

    Real environment variables win, so an inline `OPENROUTER_API_KEY=... cmd`
    or an exported key still overrides the file. Supports `export KEY=value`,
    `#` comments, and single/double quoted values.
    """
    if not os.path.exists(path):
        return {}
    loaded = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
                loaded[key] = value
    return loaded


def script_args():
    argv = sys.argv[1:]
    if "--pass" in argv:
        passed = argv[argv.index("--pass") + 1:]
        return shlex.split(" ".join(passed))
    env = os.environ.get("EVAL_ARGS")
    if env:
        return shlex.split(env)
    # freecadcmd leaves the script path in argv[0] slot handling to FreeCAD;
    # drop anything that is a path to this script or an option FreeCAD ate.
    return [arg for arg in argv if not arg.endswith(".py")]
