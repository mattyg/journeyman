import os
import xml.dom.minidom

from freecad.journeyman.resources import icon_dir, icon_path

# The preferences group passed to Gui.addPreferencePage in init_gui.py.
PREF_GROUP = "Journeyman"


def test_icon_dir_exists():
    assert os.path.isdir(icon_dir())


def test_preferences_group_icon_matches_group_name():
    """FreeCAD resolves a preferences group's icon by name, searching the
    registered icon paths for "preferences-<group>" with the group lowercased
    and spaces turned into underscores. If either the group name in
    init_gui.py or this filename changes without the other, the icon silently
    stops appearing — so pin the mapping."""
    expected = "preferences-%s.svg" % PREF_GROUP.lower().replace(" ", "_")
    assert os.path.exists(os.path.join(icon_dir(), expected))


def test_group_icon_paints_with_a_literal_colour():
    """The dialog rasterises the group icon without a stylesheet context, so a
    currentColor with no fallback would render black instead of brass. Check
    the paint attributes rather than the raw text, so prose in the file's
    comments doesn't decide the result."""
    path = os.path.join(icon_dir(), "preferences-journeyman.svg")
    doc = xml.dom.minidom.parse(path)
    painted = 0
    for node in doc.getElementsByTagName("*"):
        for attr in ("fill", "stroke"):
            value = node.getAttribute(attr)
            if value and value != "none":
                assert value != "currentColor", "%s=%s in %s" % (
                    attr, value, os.path.basename(path))
                painted += 1
    assert painted, "no painted geometry found"


def test_small_sizes_use_the_small_master():
    """Scaling the 64px master down to toolbar size closes up the J's hook and
    erases the spark's points, so small sizes get their own drawing."""
    assert icon_path(16).endswith("Journeyman-16.svg")
    assert icon_path(24).endswith("Journeyman.svg")
    assert icon_path().endswith("Journeyman.svg")


def test_png_fallback_when_svg_not_wanted():
    assert icon_path(16, prefer_svg=False).endswith(".png")
    assert icon_path(128, prefer_svg=False).endswith(".png")


def test_all_bundled_svgs_are_valid_xml():
    for name in os.listdir(icon_dir()):
        if name.endswith(".svg"):
            xml.dom.minidom.parse(os.path.join(icon_dir(), name))
