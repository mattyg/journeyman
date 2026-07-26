"""Namespace package for the Journeyman addon."""

import importlib
import sys

# Compatibility aliases for integrations written against the former flat
# package. Implementations live in the transcript domain package.
from .transcript import export as transcript_export
from .transcript import storage as history_store
from .workflow import feedback as turn_protocol
from .workflow import policy as cad_workflow

document_inspector = importlib.import_module(".document.state", __name__)
document_session = importlib.import_module(".document.session", __name__)
script_executor = importlib.import_module(".document.execution", __name__)
view_capture = importlib.import_module(".visual.capture", __name__)
settings = importlib.import_module(".config.settings", __name__)
try:
    image_processing = importlib.import_module(".visual.reference", __name__)
except ModuleNotFoundError as exc:
    if exc.name != "PySide":
        raise
    image_processing = None

# Preserve imports such as ``freecad.journeyman.settings`` without retaining
# top-level shim files. New code should import from the domain packages.
for legacy_name, module in {
        "document_inspector": document_inspector,
        "document_session": document_session,
        "script_executor": script_executor,
        "view_capture": view_capture,
        "settings": settings,
}.items():
    sys.modules[__name__ + "." + legacy_name] = module
if image_processing is not None:
    sys.modules[__name__ + ".image_processing"] = image_processing

__all__ = [
    "history_store", "transcript_export", "turn_protocol", "cad_workflow",
    "document_inspector", "document_session", "script_executor",
    "view_capture", "image_processing", "settings",
]
