# freecad/llm_copilot/init_gui.py
#
# Non-workbench startup module. FreeCAD executes every Mod/<addon>/InitGui.py at
# GUI startup regardless of addon type, so we register everything here without
# defining a Gui.Workbench subclass. The copilot is an ambient dock widget
# available in every workbench (toggled via View -> Panels), not a mode you
# switch into. Settings live on a standard FreeCAD preferences page.

import os

import FreeCAD
import FreeCADGui as Gui

_HERE = os.path.dirname(__file__)
_PREF_UI = os.path.join(_HERE, "Resources", "preferences", "llmcopilot.ui")


def _register_preference_page():
    """Add the settings page under Edit -> Preferences."""
    try:
        Gui.addPreferencePage(_PREF_UI, "LLM Copilot")
    except Exception as exc:  # never let a UI-registration hiccup abort startup
        FreeCAD.Console.PrintWarning(
            "LLM Copilot: could not register preferences page: %s\n" % exc)


def _install_panel():
    """Ensure runtime deps, then install the (hidden) dock widget."""
    from freecad.llm_copilot.deps import ensure_deps
    ensure_deps()
    from freecad.llm_copilot.chat_panel import create_panel
    create_panel(visible=False)


# addPreferencePage can run as soon as the GUI is up. Creating the dock touches
# the main window, so defer it until the first workbench activation (guaranteed
# to run after the main window exists).
_register_preference_page()


def _on_start(_name=None):
    try:
        _install_panel()
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            "LLM Copilot: could not install panel: %s\n" % exc)
    finally:
        # one-shot: detach after first activation
        try:
            Gui.getMainWindow().workbenchActivated.disconnect(_on_start)
        except Exception:
            pass


# The main window exists by the time any workbench is activated; hook the first
# activation to install the dock, then disconnect.
try:
    Gui.getMainWindow().workbenchActivated.connect(_on_start)
except Exception:
    # Fallback: try installing immediately (e.g. if the signal is unavailable).
    _install_panel()
