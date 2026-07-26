"""Document state, execution, and session façade."""

from .execution import ExecResult
from .session import DocumentSession, PinnedDocumentApp, run_with_document


def __getattr__(name):
    if name in {"DocumentDelta", "DocumentState", "document_state"}:
        from . import state
        return getattr(state, name)
    raise AttributeError(name)

__all__ = [
    "ExecResult", "DocumentSession", "PinnedDocumentApp", "run_with_document",
    "DocumentDelta", "DocumentState", "document_state",
]
