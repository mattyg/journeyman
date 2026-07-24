"""Dependency check for the LLM Copilot addon.

The copilot's LLM client is written against the Python standard library only
(urllib + json), so there is nothing to pip-install into FreeCAD's Python. This
module verifies that the stdlib pieces the client relies on are importable and
returns True; it exists so the workbench has a single, stable place to gate on
runtime prerequisites if that ever changes.
"""

import importlib

# Standard-library modules the LLM client depends on. All ship with CPython, so
# this should always succeed inside FreeCAD's interpreter.
_REQUIRED = ("urllib.request", "json")

GUIDANCE = (
    "LLM Copilot could not import a required standard-library module. This "
    "usually means FreeCAD is running on a stripped-down Python build. Please "
    "report this with your FreeCAD version."
)


def _can_import(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def ensure_deps() -> bool:
    """Return True if all runtime dependencies are importable.

    No third-party packages are required — the client uses only the stdlib.
    """
    if all(_can_import(name) for name in _REQUIRED):
        return True
    try:
        import FreeCAD
        FreeCAD.Console.PrintWarning(GUIDANCE + "\n")
    except Exception:
        print(GUIDANCE)
    return False
