# tests/integration/test_freecad_adapters.py  (run under freecadcmd)
import unittest
import FreeCAD as App
from freecad.llm_copilot import document_inspector as di

class InspectorTests(unittest.TestCase):
    def tearDown(self):
        for d in list(App.listDocuments()):
            App.closeDocument(d)

    def test_no_active_document(self):
        snap = di.snapshot(App)
        self.assertTrue(snap.startswith("NO_ACTIVE_DOCUMENT"))
        self.assertIn("newDocument", snap)

    def test_lists_objects_with_bbox(self):
        doc = App.newDocument("T")
        box = doc.addObject("Part::Box", "Box")
        doc.recompute()
        snap = di.snapshot(App)
        self.assertIn("Box", snap)
        self.assertIn("Part::Box", snap)
        self.assertIn("BoundBox", snap)


# add to tests/integration/test_freecad_adapters.py
from freecad.llm_copilot import script_executor as se

class ExecutorTests(unittest.TestCase):
    def setUp(self): self.doc = App.newDocument("E")
    def tearDown(self):
        for d in list(App.listDocuments()): App.closeDocument(d)

    def test_run_creates_object_in_one_transaction(self):
        r = se.run(App, "App.ActiveDocument.addObject('Part::Box','B')")
        self.assertTrue(r.ok, r.error)
        self.assertIsNotNone(self.doc.getObject("B"))
        se.undo(App)  # single undo removes it
        self.assertIsNone(self.doc.getObject("B"))

    def test_error_aborts_and_reports(self):
        before = len(self.doc.Objects)
        r = se.run(App, "raise ValueError('boom')")
        self.assertFalse(r.ok)
        self.assertIn("boom", r.error)
        self.assertEqual(len(self.doc.Objects), before)  # nothing half-applied

    def test_stdout_captured(self):
        r = se.run(App, "print('hello-from-script')")
        self.assertTrue(r.ok)
        self.assertIn("hello-from-script", r.output)
