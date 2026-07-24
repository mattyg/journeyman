from types import SimpleNamespace

from freecad.llm_copilot.document_binding import (
    PinnedDocumentApp, run_with_document)


class FakeFreeCAD:
    def __init__(self, documents, active):
        self.documents = {doc.Name: doc for doc in documents}
        self.ActiveDocument = active
        self.Console = object()

    def setActiveDocument(self, name):
        self.ActiveDocument = self.documents.get(name)


def test_pinned_app_keeps_agent_document_while_delegating_module_attributes():
    first = SimpleNamespace(Name="First")
    second = SimpleNamespace(Name="Second")
    module = FakeFreeCAD([first, second], second)
    app = PinnedDocumentApp(module, first)
    assert app.ActiveDocument is first
    assert app.Console is module.Console


def test_run_with_document_restores_visible_document():
    first = SimpleNamespace(Name="First")
    second = SimpleNamespace(Name="Second")
    module = FakeFreeCAD([first, second], second)
    seen = run_with_document(
        module, first, lambda: module.ActiveDocument.Name)
    assert seen == "First"
    assert module.ActiveDocument is second


def test_run_with_document_restores_no_active_document():
    first = SimpleNamespace(Name="First")
    module = FakeFreeCAD([first], None)
    run_with_document(module, first, lambda: None)
    assert module.ActiveDocument is None
