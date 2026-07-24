"""Bind one agent to one FreeCAD document while UI document selection changes."""


class PinnedDocumentApp:
    """Proxy a FreeCAD module but keep ActiveDocument fixed for an agent."""

    def __init__(self, module, document):
        self._module = module
        self.ActiveDocument = document

    def __getattr__(self, name):
        return getattr(self._module, name)


def run_with_document(module, document, fn):
    """Run a GUI-sensitive operation with the owning document temporarily active."""
    previous = getattr(module, "ActiveDocument", None)
    try:
        if document is not None and previous is not document:
            module.setActiveDocument(document.Name)
        return fn()
    finally:
        if previous is not document:
            module.setActiveDocument(previous.Name if previous is not None else "")
