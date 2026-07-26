# tests/integration/test_freecad_adapters.py  (run under freecadcmd)
import unittest
import os
import tempfile
import FreeCAD as App
from freecad.journeyman import document_inspector as di
from freecad.journeyman import view_capture
from freecad.journeyman.transcript import storage as history_store
from freecad.journeyman import api_reference
from freecad.journeyman.script_executor import ExecResult
from freecad.journeyman.document_session import (
    PinnedDocumentApp, run_with_document)
from freecad.journeyman.image_processing import (
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

    def test_rich_snapshot_is_flat_text_not_json(self):
        doc = App.newDocument("T")
        box = doc.addObject("Part::Box", "Box")
        box.Length = 10
        doc.recompute()
        snap = di.snapshot(App, rich=True)
        self.assertTrue(snap.startswith("[rich state]\n"))
        self.assertIn("Box (Part::Box)", snap)
        self.assertIn("solids=1", snap)
        self.assertNotIn('"type":', snap)
        # The denylist strips chrome the model never acts on.
        self.assertNotIn("Visibility=", snap)

    def test_inspect_query_matches_label_and_type_not_just_name(self):
        doc = App.newDocument("T")
        box = doc.addObject("Part::Box", "Box")
        box.Label = "Baseplate"
        doc.recompute()
        doc.addObject("Part::Cylinder", "Cylinder")
        doc.recompute()
        # Neither query names "Box" literally; both must still find it.
        self.assertIn("Box (Part::Box)", di.inspect(App, "the baseplate"))
        self.assertIn("Box (Part::Box)", di.inspect(App, "part::box object"))

    def test_cylinder_diameters_are_measured_from_the_solid(self):
        """A run measured its own model, saw no 12 mm cylinder where the bolt
        hole should be, and talked itself out of the discrepancy. The number
        must come from the solid, not from a sketch bounding box."""
        doc = App.newDocument("Holes")
        box = doc.addObject("Part::Box", "Plate")
        box.Length, box.Width, box.Height = 40, 40, 4
        cyl = doc.addObject("Part::Cylinder", "Bolt")
        cyl.Radius, cyl.Height = 6.0, 10.0
        cyl.Placement.Base = App.Vector(20, 20, -2)
        cut = doc.addObject("Part::Cut", "Drilled")
        cut.Base, cut.Tool = box, cyl
        doc.recompute()
        state = di.document_state(App, rich=True)
        holes = state["objects"]["Drilled"]["shape"]["cylinder_diameters"]
        self.assertIn(12.0, holes)
        # And it reaches the model through the validation summary.
        ok, report = di.validate(App, names=["Drilled"])
        self.assertTrue(ok, report)
        self.assertIn("cylinder_diameters", report)
        self.assertIn("12.0", report)

    def test_shapes_without_cylinders_omit_the_key(self):
        doc = App.newDocument("NoHoles")
        box = doc.addObject("Part::Box", "Plain")
        doc.recompute()
        shape = di.document_state(App, rich=True)["objects"]["Plain"]["shape"]
        self.assertNotIn("cylinder_diameters", shape)

    def test_validation_failure_error_names_the_broken_object(self):
        doc = App.newDocument("Bad")
        feature = doc.addObject("PartDesign::Feature", "Broken")
        doc.recompute()
        result = se.run(
            App, "pass", validate=True, rollback_on_failure=True)
        if not result.ok:
            self.assertIn("rolled back", result.error)
            self.assertNotEqual(
                result.error.strip(), "POST_EXECUTION_VALIDATION_FAILED")

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

    def test_on_demand_render_tool_isolates_a_named_body(self):
        """The render tool end-to-end against real geometry.

        Unit tests stub the capture callback, so only this proves the tool's
        name filtering reaches _final_shape_objects and produces real pixels.
        """
        from freecad.journeyman.agent import Agent
        from freecad.journeyman.llm_client import LLMProposal
        from freecad.journeyman.settings import Settings

        doc = App.newDocument("T")
        doc.addObject("Part::Box", "Keep")
        far = doc.addObject("Part::Box", "Skip")
        far.Placement.Base.x = 40
        doc.recompute()

        def capture(names, strategy, limit, **options):
            return view_capture.capture(
                App, names, strategy=strategy, max_isolated=limit, **options)

        agent = Agent(
            client=None, inspector=lambda _app: "DOC", executor=None,
            app=App, settings=Settings("m", "", "", True, False, 5, 3),
            view_capture=capture)
        agent._handle_render(
            LLMProposal("", "", "", False, kind="render",
                        render_objects=("Keep",)), None)

        content = agent.messages[-1]["content"]
        self.assertIsInstance(content, list)
        labels = [b["text"] for b in content if b["type"] == "text"]
        self.assertTrue(any("Keep" in label for label in labels))
        self.assertFalse(any("Skip" in label for label in labels))
        images = [b for b in content if b["type"] == "image_url"]
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0]["image_url"]["url"].startswith(
            "data:image/png;base64,iVBOR"))

    def test_on_demand_render_reports_an_unknown_object_name(self):
        from freecad.journeyman.agent import Agent
        from freecad.journeyman.llm_client import LLMProposal
        from freecad.journeyman.settings import Settings

        App.newDocument("T")

        def capture(names, strategy, limit, **options):
            return view_capture.capture(
                App, names, strategy=strategy, max_isolated=limit, **options)

        agent = Agent(
            client=None, inspector=lambda _app: "DOC", executor=None,
            app=App, settings=Settings("m", "", "", True, False, 5, 3),
            view_capture=capture)
        agent._handle_render(
            LLMProposal("", "", "", False, kind="render",
                        render_objects=("NoSuchBody",)), None)
        content = agent.messages[-1]["content"]
        self.assertIsInstance(content, str)
        self.assertIn("NoSuchBody", content)

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

    def test_legacy_history_object_is_hidden_from_the_snapshot(self):
        """A document saved before the rename carries LLMCopilotChatHistory.

        Unrecognised, its compressed payload went into every snapshot — one
        real document put ~367k tokens of base64 in front of the model.
        """
        doc = App.newDocument("T")
        doc.addObject("Part::Box", "Box")
        obj = doc.addObject("App::FeaturePython", "LLMCopilotChatHistory")
        obj.addProperty("App::PropertyString", "Payload")
        obj.Payload = "eNr" + "A" * 200000
        doc.recompute()
        snapshot = di.snapshot(App, rich=True)
        self.assertNotIn("LLMCopilotChatHistory", snapshot)
        self.assertNotIn("eNrAAA", snapshot)
        self.assertIn("Box", snapshot)
        self.assertLess(len(snapshot), 10000)

    def test_an_oversized_property_is_truncated_not_dumped(self):
        """Size is the backstop for whatever the name filter misses next."""
        doc = App.newDocument("T")
        box = doc.addObject("Part::Box", "Box")
        box.addProperty("App::PropertyString", "Blob")
        box.Blob = "Z" * 100000
        doc.recompute()
        snapshot = di.snapshot(App, rich=True)
        self.assertIn("truncated, 100000 chars", snapshot)
        self.assertLess(len(snapshot), 10000)

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
from freecad.journeyman import script_executor as se

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

    def test_cleanup_survives_a_script_that_raises_afterwards(self):
        """Work completed before an error is kept, not undone.

        climbing-hanger-transcript-3 spent ~8 turns here: every repair script
        deleted a corrupt object, rebuilt it, then failed an assertion — and
        the abort erased the repair, so the next attempt met the same corrupt
        state. Only the real transaction can prove this.
        """
        self.doc.addObject("Part::Box", "Corrupt")
        self.doc.recompute()
        result = se.run(App, "\n".join([
            "doc = App.ActiveDocument",
            "doc.removeObject('Corrupt')",
            "doc.addObject('Part::Box', 'Rebuilt')",
            "print('repaired')",
            "raise ValueError('assertion failed after the repair')",
        ]))
        self.assertFalse(result.ok)
        self.assertIn("assertion failed after the repair", result.error)
        self.assertIn("repaired", result.output)
        # The repair survived: the corrupt object is gone and the new one exists.
        self.assertIsNone(self.doc.getObject("Corrupt"))
        self.assertIsNotNone(self.doc.getObject("Rebuilt"))
        self.assertFalse(result.rolled_back)

    def test_opting_out_rolls_a_failed_script_back(self):
        self.doc.addObject("Part::Box", "Keep")
        self.doc.recompute()
        result = se.run(App, "\n".join([
            "App.ActiveDocument.removeObject('Keep')",
            "raise ValueError('boom')",
        ]), keep_partial_on_error=False)
        self.assertFalse(result.ok)
        self.assertTrue(result.rolled_back)
        self.assertIsNotNone(self.doc.getObject("Keep"))

    def test_partial_work_remains_undoable_by_the_user(self):
        result = se.run(App, "\n".join([
            "App.ActiveDocument.addObject('Part::Box', 'Half')",
            "raise ValueError('stopped')",
        ]))
        self.assertFalse(result.ok)
        self.assertIsNotNone(self.doc.getObject("Half"))
        se.undo(App)
        self.assertIsNone(self.doc.getObject("Half"))

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
