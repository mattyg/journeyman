"""FreeCAD rendering façade.

Reference-image processing is intentionally not imported here because it needs
PySide, while geometry capture and the pure test suite do not.
"""

from .capture import capture

__all__ = ["capture"]
