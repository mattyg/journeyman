# freecad/journeyman/init_gui.py
#
# Non-workbench startup module. FreeCAD executes every Mod/<addon>/InitGui.py at
# GUI startup regardless of addon type, so we register everything here without
# defining a Gui.Workbench subclass. Journeyman is an ambient dock widget
# available in every workbench (toggled via View -> Panels), not a mode you
# switch into. Settings live on a standard FreeCAD preferences page.

import FreeCAD
import FreeCADGui as Gui


def _register_icon_path():
    """Make the bundled icons resolvable by name.

    FreeCAD's preferences dialog finds a group's icon by searching the
    registered icon paths for "preferences-<group>", lowercased with spaces
    turned into underscores — so registering this directory is what lets
    preferences-journeyman.svg appear next to our page. Core workbenches use a
    compiled Qt resource path (":/icons") here; a pure-Python addon has no
    .qrc, so we register the filesystem directory, as the Addon Manager does.
    """
    try:
        from freecad.journeyman.resources import icon_dir
        Gui.addIconPath(icon_dir())
    except Exception as exc:  # a missing icon must not break startup
        FreeCAD.Console.PrintWarning(
            "Journeyman: could not register icon path: %s\n" % exc)


def _register_preference_page():
    """Add the settings page under Edit -> Preferences.

    Uses the class-based form of addPreferencePage (as FreeCAD's own Assembly
    and CAM workbenches do) so the page can populate its Model dropdown from a
    live provider fetch — something a static .ui page cannot do."""
    try:
        from freecad.journeyman.preferences import JourneymanPreferencesPage
        Gui.addPreferencePage(JourneymanPreferencesPage, "Journeyman")
    except Exception as exc:  # never let a UI-registration hiccup abort startup
        FreeCAD.Console.PrintWarning(
            "Journeyman: could not register preferences page: %s\n" % exc)


def _install_panel():
    """Ensure runtime deps, then install the (hidden) dock widget."""
    from freecad.journeyman.deps import ensure_deps
    ensure_deps()
    from freecad.journeyman.chat_panel import create_panel
    create_panel(visible=False)


# addPreferencePage can run as soon as the GUI is up. Creating the dock touches
# the main window, so defer it until the first workbench activation (guaranteed
# to run after the main window exists). The icon path has to be registered
# first, or the dialog cannot resolve our group icon.
_register_icon_path()
_register_preference_page()


def _on_start(_name=None):
    try:
        _install_panel()
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            "Journeyman: could not install panel: %s\n" % exc)
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
