# tests/integration/test_freecad_adapters.py  (run under freecadcmd)
import unittest
import os
import tempfile
import FreeCAD as App
from freecad.llm_copilot import document_inspector as di
from freecad.llm_copilot import view_capture
from freecad.llm_copilot import history_store
from freecad.llm_copilot import api_reference
from freecad.llm_copilot.types import ExecResult
from freecad.llm_copilot.document_binding import (
    PinnedDocumentApp, run_with_document)
from freecad.llm_copilot.image_processing import (
    contrast_enhanced, edge_enhanced)

class InspectorTests(unittest.TestCase):
    def tearDown(self):
        for d in list(App.listDocuments()):
            App.closeDocument(d)

    def test_no_active_document(self):
        snap = di.snapshot(App)
        self.assertTrue(snap.startswith("NO_ACTIVE_DOCUMENT"))
        self.assertIn("create or open", snap)

    def test_reference_contrast_and_edge_enhancements(self):
        from PySide import QtGui
        image = QtGui.QImage(40, 30, QtGui.QImage.Format_ARGB32)
        image.fill(QtGui.QColor(180, 180, 180))
        for y in range(8, 22):
            for x in range(12, 28):
                image.setPixel(x, y, QtGui.QColor(40, 40, 40).rgba())
        contrast = contrast_enhanced(image)
        edges = edge_enhanced(image)
        self.assertEqual(contrast.size(), image.size())
        self.assertEqual(edges.size(), image.size())
        self.assertNotEqual(
            image.pixel(12, 8), edges.pixel(12, 8))

    def test_lists_objects_with_bbox(self):
        doc = App.newDocument("T")
        box = doc.addObject("Part::Box", "Box")
        doc.recompute()
        snap = di.snapshot(App)
        self.assertIn("Box", snap)
        self.assertIn("Part::Box", snap)
        self.assertIn("BoundBox", snap)

    def test_rich_state_and_diff(self):
        doc = App.newDocument("T")
        before = di.document_state(App, rich=True)
        box = doc.addObject("Part::Box", "Box")
        box.Length = 10
        doc.recompute()
        after = di.document_state(App, rich=True)
        self.assertTrue(after["objects"]["Box"]["shape"]["valid"])
        self.assertIn("Created: Box", di.structured_diff(before, after))

    def test_validation_reports_valid_solid(self):
        doc = App.newDocument("T")
        doc.addObject("Part::Box", "Box")
        doc.recompute()
        ok, report = di.validate(App)
        self.assertTrue(ok, report)
        self.assertIn("solids=1", report)

    def test_empty_body_and_origin_are_valid_structural_objects(self):
        doc = App.newDocument("T")
        doc.addObject("PartDesign::Body", "Body")
        doc.recompute()
        ok, report = di.validate(App)
        self.assertTrue(ok, report)

    def test_offscreen_element_render_works_without_active_view(self):
        doc = App.newDocument("T")
        doc.addObject("Part::Box", "Box")
        doc.recompute()
        images = view_capture.capture(
            App, ["Box"], strategy="changed", max_isolated=1)
        self.assertEqual(len(images), 1)
        self.assertIn("Box", images[0]["label"])
        self.assertIn("front, back, left", images[0]["label"])
        self.assertIn("right, top, bottom", images[0]["label"])
        self.assertTrue(images[0]["data"].startswith("iVBOR"))

    def test_technical_render_adds_edges_colors_and_depth_shading(self):
        doc = App.newDocument("T")
        first = doc.addObject("Part::Box", "First")
        second = doc.addObject("Part::Box", "Second")
        second.Placement.Base.x = 14
        doc.recompute()
        technical = view_capture._render_pixels(
            [first, second], (1, -1, -1), 192, 128,
            technical_edges=True, object_colors=True, depth_shading=True)
        neutral = view_capture._render_pixels(
            [first, second], (1, -1, -1), 192, 128,
            technical_edges=False, object_colors=False, depth_shading=False)
        self.assertIsNotNone(technical)
        self.assertIn(bytes(view_capture._EDGE_COLOR), bytes(technical))
        self.assertNotEqual(technical, neutral)

    def test_installed_version_api_lookup_is_bounded_and_versioned(self):
        result = api_reference.lookup(
            App, "create a box primitive", "Part", "makeBox")
        self.assertIn("Installed FreeCAD version:", result)
        self.assertIn("Part.makeBox", result)
        self.assertLessEqual(len(result), 12000)

    def test_history_persists_inside_saved_document_and_is_hidden(self):
        doc = App.newDocument("T")
        doc.addObject("Part::Box", "Box")
        history_store.save(
            doc, [{"role": "user", "content": "remember me"}],
            [{"kind": "step", "id": 1, "intent": "Box", "script": "pass",
              "result": ExecResult(True, "", "")}])
        snapshot = di.snapshot(App, rich=True)
        self.assertNotIn(history_store.OBJECT_NAME, snapshot)
        handle, path = tempfile.mkstemp(suffix=".FCStd")
        os.close(handle)
        try:
            doc.saveAs(path)
            App.closeDocument("T")
            reopened = App.openDocument(path)
            messages, entries = history_store.load(reopened)
            self.assertEqual(messages[0]["content"], "remember me")
            self.assertEqual(entries[0]["intent"], "Box")
            history_store.clear(reopened)
            self.assertIsNone(reopened.getObject(history_store.OBJECT_NAME))
        finally:
            if os.path.exists(path):
                os.unlink(path)


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

    def test_pinned_agent_edits_owner_and_restores_visible_document(self):
        visible = App.newDocument("Visible")
        App.setActiveDocument(visible.Name)
        pinned = PinnedDocumentApp(App, self.doc)
        result = run_with_document(
            App, self.doc,
            lambda: se.run(
                pinned,
                "App.ActiveDocument.addObject('Part::Box','OwnedBox')"))
        self.assertTrue(result.ok, result.error)
        self.assertIsNotNone(self.doc.getObject("OwnedBox"))
        self.assertIsNone(visible.getObject("OwnedBox"))
        self.assertIs(App.ActiveDocument, visible)

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

    def test_stderr_and_freecad_console_diagnostics_are_scoped(self):
        r = se.run(
            App,
            "import sys\n"
            "sys.stderr.write('stderr detail\\n')\n"
            "App.Console.PrintWarning('warning detail\\n')\n"
            "App.Console.PrintError('error detail\\n')\n")
        self.assertTrue(r.ok)
        self.assertIn("stderr detail", r.stderr)
        self.assertIn("warning detail", r.console_warnings)
        self.assertIn("error detail", r.console_errors)
        clean = se.run(App, "print('clean')")
        self.assertEqual(clean.stderr, "")
        self.assertEqual(clean.console_warnings, "")
        self.assertEqual(clean.console_errors, "")

    def test_validation_runs_before_commit(self):
        r = se.run(
            App, "App.ActiveDocument.addObject('Part::Box','B')",
            validate=True, rollback_on_failure=True)
        self.assertTrue(r.ok, r.validation)
        self.assertTrue(r.validation_ok)
        self.assertIn("valid=True", r.validation)

    def test_invalid_shape_is_rolled_back(self):
        r = se.run(
            App, "App.ActiveDocument.addObject('PartDesign::Feature','Broken')",
            validate=True, rollback_on_failure=True)
        self.assertFalse(r.ok)
        self.assertTrue(r.rolled_back)
        self.assertIn("shape", r.validation)
        self.assertIsNone(self.doc.getObject("Broken"))

    def test_read_only_diagnostic_does_not_validate_existing_origin(self):
        self.doc.addObject("PartDesign::Body", "Body")
        self.doc.recompute()
        r = se.run(
            App, "print([o.Name for o in App.ActiveDocument.Objects])",
            validate=True, rollback_on_failure=True)
        self.assertTrue(r.ok, r.error + "\n" + r.validation)
        self.assertIn("No document changes", r.validation)
        self.assertIsNotNone(self.doc.getObject("Origin"))

    def test_persisting_history_does_not_interfere_with_geometry_undo(self):
        r = se.run(App, "App.ActiveDocument.addObject('Part::Box','B')")
        self.assertTrue(r.ok, r.error)
        history_store.save(
            self.doc, [{"role": "user", "content": "box"}], [])
        se.undo(App)
        self.assertIsNone(self.doc.getObject("B"))
        self.assertIsNotNone(self.doc.getObject(history_store.OBJECT_NAME))
