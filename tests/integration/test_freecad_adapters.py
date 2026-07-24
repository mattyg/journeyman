# tests/integration/test_freecad_adapters.py  (run under freecadcmd)
import unittest
import FreeCAD as App
from freecad.llm_copilot import document_inspector as di

class InspectorTests(unittest.TestCase):
    def tearDown(self):
        for d in list(App.listDocuments()):
            App.closeDocument(d)

    def test_no_active_document(self):
        self.assertEqual(di.snapshot(App), "NO_ACTIVE_DOCUMENT")

    def test_lists_objects_with_bbox(self):
        doc = App.newDocument("T")
        box = doc.addObject("Part::Box", "Box")
        doc.recompute()
        snap = di.snapshot(App)
        self.assertIn("Box", snap)
        self.assertIn("Part::Box", snap)
        self.assertIn("BoundBox", snap)
