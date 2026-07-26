"""Bundled icon assets and path helpers.

Icons ship as SVG (the format FreeCAD and the Addon Manager prefer, since it
scales to whatever the host's device-pixel-ratio demands) with PNG renders
alongside for anything that cannot load SVG.

There are two SVG masters, not one: ``Journeyman.svg`` for 24px and up, and
``Journeyman-16.svg`` redrawn with lighter strokes for toolbar sizes. Scaling
the master down to 16px fills in the hook and erases the spark's points, so
``icon_path`` picks the right master for the requested size.
"""

import os

_ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")

# Below this, the master's stroke weight closes up the J's hook.
_SMALL_CUTOFF = 20


def icon_dir():
    """Absolute path to the bundled icon directory."""
    return _ICON_DIR


def icon_path(size=None, prefer_svg=True):
    """Return a path to the Journeyman mark.

    ``size`` is the pixel size the icon will be drawn at, and selects between
    the small and standard masters; ``None`` returns the standard master.
    Falls back to PNG when the requested SVG is missing.
    """
    small = size is not None and size < _SMALL_CUTOFF
    stem = "Journeyman-16" if small else "Journeyman"

    if prefer_svg:
        svg = os.path.join(_ICON_DIR, stem + ".svg")
        if os.path.exists(svg):
            return svg

    # PNG fallback: prefer an exact-size render, else the largest available.
    if size is not None:
        exact = os.path.join(_ICON_DIR, "Journeyman-%d.png" % size)
        if os.path.exists(exact):
            return exact
    for candidate in ("Journeyman-256.png", "Journeyman-128.png",
                      "Journeyman-64.png", "Journeyman-32.png"):
        path = os.path.join(_ICON_DIR, candidate)
        if os.path.exists(path):
            return path
    return None
