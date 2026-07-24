import importlib

GUIDANCE = (
    "LLM Copilot requires the 'litellm' package. Install it into FreeCAD's "
    "Python environment, e.g.:\n"
    "    <freecad-python> -m pip install -r requirements.txt\n"
    "then restart FreeCAD."
)

def _can_import(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False

def ensure_litellm() -> bool:
    if _can_import("litellm"):
        return True
    try:
        import FreeCAD
        FreeCAD.Console.PrintWarning(GUIDANCE + "\n")
    except Exception:
        print(GUIDANCE)
    return False
