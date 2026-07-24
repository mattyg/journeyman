from types import SimpleNamespace

from freecad.llm_copilot import cad_workflow
from freecad.llm_copilot.settings import Settings


def _settings(**changes):
    settings = Settings("m", "", "", True, False, 5, 3)
    for key, value in changes.items():
        setattr(settings, key, value)
    return settings


def test_proposal_issues_requires_complete_design_metadata():
    proposal = SimpleNamespace(
        strategy="", stage="", plan=(), plan_step=0, success_criteria=())
    assert len(cad_workflow.proposal_issues(proposal)) == 5


def test_only_part_design_strategy_is_valid():
    proposal = SimpleNamespace(
        strategy="part", stage="sketch", plan=("Sketch",), plan_step=1,
        success_criteria=("One solid",))
    assert "choose a valid CAD strategy" in cad_workflow.proposal_issues(
        proposal)


def test_review_flags_unconstrained_unattached_part_design_sketch():
    proposal = SimpleNamespace(strategy="part_design", stage="sketch")
    after = {"objects": {
        "Profile": {
            "type": "Sketcher::SketchObject",
            "fully_constrained": False,
            "properties": {"MapMode": "Deactivated"},
        },
    }}
    warnings = cad_workflow.review_step(
        {"objects": {}}, after, proposal,
        _settings(sketch_constraint_verification=True))
    assert any("not fully constrained" in warning for warning in warnings)
    assert any("stable attachment" in warning for warning in warnings)


def test_ledger_is_compact_and_records_warnings():
    text = cad_workflow.ledger_text({
        "strategy": "part_design", "stage": "sketch",
        "plan": ("Sketch profile", "Pad it"), "completed_steps": 1,
        "success_criteria": ("One solid",),
        "completed_stages": {"sketch"},
        "warnings": ("Profile is under-constrained",),
    })
    assert "✓ Sketch profile" in text
    assert "○ Pad it" in text
    assert "Profile is under-constrained" in text


def _assumption(**changes):
    row = {
        "id": "width", "name": "Width", "value": 20.0, "unit": "mm",
        "source": "estimated from photo", "confidence": "medium",
        "consequence": "high", "if_wrong": "overall scale is wrong",
        "status": "unverified", "evidence": "",
    }
    row.update(changes)
    return row


def test_assumption_ledger_validation_and_blocking():
    turn = SimpleNamespace(assumptions_accepted=False)
    assert cad_workflow.assumption_ledger_missing(
        SimpleNamespace(assumptions=None), turn)
    assert not cad_workflow.assumption_ledger_missing(
        SimpleNamespace(assumptions=()), turn)
    rows = (_assumption(confidence="low"),)
    assert not cad_workflow.assumption_ledger_missing(
        SimpleNamespace(assumptions=rows), turn)
    assert cad_workflow.blocking_assumptions(rows) == rows


def test_assumption_ledger_rejects_duplicate_ids():
    rows = (
        _assumption(consequence="low"),
        _assumption(consequence="high"),
    )
    issues = cad_workflow.assumption_ledger_missing(
        SimpleNamespace(assumptions=rows),
        SimpleNamespace(assumptions_accepted=False))
    assert any("duplicate" in issue for issue in issues)
    # Row order is no longer a defect: it is presentation, and the host sorts it.
    assert not any("sorted" in issue for issue in issues)


def test_row_order_never_blocks_a_step():
    """A mis-sorted ledger deadlocked a real run; sorting is the host's job."""
    rows = (
        _assumption(id="a", consequence="low"),
        _assumption(id="b", consequence="high"),
        _assumption(id="c", consequence="medium"),
    )
    assert cad_workflow.assumption_ledger_missing(
        SimpleNamespace(assumptions=rows),
        SimpleNamespace(assumptions_accepted=False)) == []


def test_sort_assumptions_orders_most_severe_first_and_is_stable():
    rows = (
        _assumption(id="a", consequence="low"),
        _assumption(id="b", consequence="high"),
        _assumption(id="c", consequence="medium"),
        _assumption(id="d", consequence="high"),
    )
    assert [row["id"] for row in cad_workflow.sort_assumptions(rows)] == [
        "b", "d", "c", "a"]


def test_sort_assumptions_puts_invalid_consequence_last():
    rows = (
        _assumption(id="bad", consequence="CRITICAL"),
        _assumption(id="ok", consequence="high"),
    )
    assert [row["id"] for row in cad_workflow.sort_assumptions(rows)] == [
        "ok", "bad"]


def test_merged_ledger_comes_back_sorted():
    proposed = (
        _assumption(id="a", consequence="low"),
        _assumption(id="b", consequence="high"),
    )
    merged, issues = cad_workflow.merge_assumptions((), proposed)
    assert issues == []
    assert [row["id"] for row in merged] == ["b", "a"]


def test_merge_assumptions_requires_evidence_and_ledger_renders_rows():
    before = (_assumption(),)
    changed = (_assumption(value=25.0),)
    _merged, issues = cad_workflow.merge_assumptions(before, changed)
    assert any("evidence" in issue for issue in issues)
    confirmed = (_assumption(
        value=25.0, status="user_confirmed",
        evidence="User selected 25 mm"),)
    merged, issues = cad_workflow.merge_assumptions(before, confirmed)
    assert not issues
    text = cad_workflow.ledger_text({"assumptions": merged})
    assert "Width=25.0 mm" in text
    assert "user_confirmed" in text


def test_replica_features_cannot_be_removed_or_marked_done_without_evidence():
    planned = ({
        "id": "bend", "description": "Bent lower clip",
        "status": "planned", "evidence": "",
    },)
    implemented = ({
        "id": "bend", "description": "Bent lower clip",
        "status": "implemented", "evidence": "Measured 25 degree bend",
    },)
    merged, issues = cad_workflow.fidelity_feature_issues(planned, implemented)
    assert not issues
    assert merged == implemented
    _merged, issues = cad_workflow.fidelity_feature_issues(planned, ())
    assert issues
    no_evidence = (dict(implemented[0], evidence=""),)
    _merged, issues = cad_workflow.fidelity_feature_issues(
        planned, no_evidence)
    assert any("evidence" in issue for issue in issues)
    _merged, issues = cad_workflow.fidelity_feature_issues(
        implemented, planned)
    assert any("regressed" in issue for issue in issues)


def test_part_design_policy_rejects_part_shortcut_and_accepts_native_tree():
    before = {"objects": {}}
    shortcut = {"objects": {
        "Box": {"type": "Part::Box", "label": "Box"},
    }}
    issues = cad_workflow.part_design_issues(before, shortcut)
    assert any("PartDesign::Body" in issue for issue in issues)
    assert any("forbidden Part workbench" in issue for issue in issues)
    native = {"objects": {
        "Body": {"type": "PartDesign::Body", "label": "Body"},
        "Sketch": {
            "type": "Sketcher::SketchObject", "label": "Sketch",
            "body": "Body"},
        "Pad": {
            "type": "PartDesign::Pad", "label": "Pad", "body": "Body"},
    }}
    assert not cad_workflow.part_design_issues(before, native)


# --- read-only script detection (gate exemption for diagnosis) ---

DIAGNOSTIC = """import FreeCAD as App
import Part
doc = App.ActiveDocument
body = doc.getObject('HangerBody')
sk = doc.getObject('PlateSketch')
print('sketch edges', len(sk.Geometry), 'closed?', sk.Shape.isClosed())
for i, g in enumerate(sk.Geometry):
    print(i, g)
"""


def test_pure_diagnostic_script_is_read_only():
    """The step the climbing-hanger transcript rejected: reads, prints, nothing else."""
    assert cad_workflow.is_read_only_script(DIAGNOSTIC) is True


def test_local_bindings_and_reads_stay_read_only():
    assert cad_workflow.is_read_only_script(
        "w = Part.Wire(sk.Shape.Edges)\nprint(w.isClosed())") is True


def test_construction_and_mutation_are_not_read_only():
    for script in (
            "pad = body.newObject('PartDesign::Pad', 'PlatePad')",
            "doc.addObject('Sketcher::SketchObject', 'S')",
            "pad.Length = 4.0",
            "sk.addGeometry(Part.LineSegment(a, b), False)",
            "sk.addConstraint(c)",
            "sk.clearGeometry()",
            "doc.recompute()",
            "obj.Placement.Base = v",
            "values[0] = 3",
            "del doc.Objects[0]"):
        assert cad_workflow.is_read_only_script(script) is False, script


def test_unparsable_script_is_never_treated_as_read_only():
    assert cad_workflow.is_read_only_script("def (:") is False


def test_inert_script_is_not_diagnosis():
    """A no-op must not slip past the planning gates by changing nothing."""
    assert cad_workflow.is_read_only_script("pass") is False
    assert cad_workflow.is_read_only_script("x = 1") is False


# --- one feature per step ---

def test_multiple_features_in_one_script_are_rejected():
    """Turn 2 of the climbing-hanger transcript: pad plus two pockets."""
    issues = cad_workflow.multi_feature_issues(
        "pad = body.newObject('PartDesign::Pad','PlatePad')\n"
        "pk = body.newObject('PartDesign::Pocket','BoltHole')\n"
        "pk2 = body.newObject('PartDesign::Pocket','TeardropPocket')\n")
    assert len(issues) == 1
    assert "3 features" in issues[0]
    assert "PartDesign::Pad" in issues[0]


def test_one_feature_with_its_scaffolding_is_allowed():
    """A Body, sketch and datums supporting a single feature are one step."""
    assert cad_workflow.multi_feature_issues(
        "body = doc.addObject('PartDesign::Body','B')\n"
        "sk = body.newObject('Sketcher::SketchObject','S')\n"
        "pad = body.newObject('PartDesign::Pad','P')\n") == []


def test_scripts_building_no_features_are_allowed():
    assert cad_workflow.multi_feature_issues("print(doc.Objects)") == []
    assert cad_workflow.multi_feature_issues("def (:") == []


# --- placeholder (no-op) block lint ---

def test_placeholder_loop_where_constraints_belong_is_flagged():
    """Turn 1 of the climbing-hanger transcript: `for i in range(4): pass`."""
    issues = cad_workflow.noop_block_issues("for i in range(4):\n    pass\n")
    assert len(issues) == 1
    assert "for block at line 1" in issues[0]


def test_placeholder_constraint_loop_is_flagged():
    """Turn 2: the constraint pairs were enumerated, then never applied."""
    issues = cad_workflow.noop_block_issues(
        "for a, b in [(g1, g2), (g2, g4)]:\n    pass\n")
    assert len(issues) == 1


def test_real_loop_bodies_are_not_flagged():
    assert cad_workflow.noop_block_issues(
        "for i in range(4):\n    sk.addConstraint(c)\n") == []
    assert cad_workflow.noop_block_issues(
        "for i, g in enumerate(sk.Geometry):\n    print(i, g)\n") == []


def test_every_noop_block_kind_is_reported_with_its_line():
    issues = cad_workflow.noop_block_issues(
        "x = 1\n"
        "if x:\n    pass\n"
        "while x:\n    pass\n")
    assert len(issues) == 2
    assert any("if block at line 2" in issue for issue in issues)
    assert any("while block at line 4" in issue for issue in issues)


def test_unparsable_script_yields_no_noop_issues():
    assert cad_workflow.noop_block_issues("def (:") == []


# --- relabel vs redefinition ---

def test_relabelled_row_is_not_an_issue():
    """Transcript 3: a complete hanger was discarded because A5 was reworded."""
    before = (_assumption(id="A5", name="Carabiner opening height", value=30.0),)
    after = (_assumption(id="A5", name="Opening height (stadium)", value=30.0),)
    _merged, issues = cad_workflow.merge_assumptions(before, after)
    assert issues == []
    notes = cad_workflow.relabelled_assumptions(before, after)
    assert len(notes) == 1
    assert "Carabiner opening height" in notes[0]
    assert "Opening height (stadium)" in notes[0]


def test_rename_with_a_value_change_is_a_redefinition():
    before = (_assumption(id="A5", name="Opening height", value=30.0),)
    after = (_assumption(
        id="A5", name="Opening width", value=20.0, evidence="measured"),)
    _merged, issues = cad_workflow.merge_assumptions(before, after)
    assert any("redefined" in issue for issue in issues)
    assert any("Opening height" in issue and "Opening width" in issue
               for issue in issues)


def test_rename_with_a_status_promotion_is_a_redefinition():
    before = (_assumption(id="A5", name="Opening height", status="unverified"),)
    after = (_assumption(
        id="A5", name="Slot height", status="user_confirmed",
        evidence="user selection"),)
    _merged, issues = cad_workflow.merge_assumptions(before, after)
    assert any("redefined" in issue for issue in issues)


def test_value_change_without_evidence_still_blocks():
    before = (_assumption(id="A5", value=30.0),)
    after = (_assumption(id="A5", value=25.0, evidence=""),)
    _merged, issues = cad_workflow.merge_assumptions(before, after)
    assert any("changed without evidence" in issue for issue in issues)


def test_unchanged_rows_produce_no_relabel_notes():
    rows = (_assumption(id="A5"),)
    assert cad_workflow.relabelled_assumptions(rows, rows) == []


# --- degradation across successful steps ---

def _state(solids=1, volume=8364.6, valid=True, bbox=None, holes=None):
    shape = {"solids": solids, "volume": volume, "valid": valid}
    if holes:
        shape["cylinder_diameters"] = holes
    item = {"type": "PartDesign::Body", "shape": shape}
    if bbox:
        item["bbox"] = bbox
    return {"objects": {"HangerBody": item}}


def test_losing_a_solid_is_reported_even_though_the_step_succeeded():
    """transcript-4 destroyed a finished hanger over six successful steps."""
    issues = cad_workflow.regression_issues(_state(), _state(solids=0, volume=0))
    assert any("solid count dropped from 1 to 0" in issue for issue in issues)


def test_newly_invalid_objects_are_named():
    after = _state(valid=False)
    issues = cad_workflow.regression_issues(_state(), after)
    assert any("now invalid: HangerBody" in issue for issue in issues)


def test_losing_most_of_the_volume_is_reported():
    issues = cad_workflow.regression_issues(_state(), _state(volume=100.0))
    assert any("material disappeared" in issue for issue in issues)


def test_an_improving_step_reports_nothing():
    assert cad_workflow.regression_issues(_state(), _state(volume=9000.0)) == []
    assert cad_workflow.regression_issues(None, _state()) == []


def test_a_pocket_removing_some_material_is_not_a_regression():
    """Cutting a hole legitimately reduces volume; only a collapse matters."""
    assert cad_workflow.regression_issues(_state(), _state(volume=8000.0)) == []


# --- "you already have a solid" summary ---

def test_buildable_summary_states_size_and_holes():
    text = cad_workflow.buildable_summary(
        _state(bbox=[35.0, 72.4, 4.0], holes=[3.0, 12.0, 30.0]))
    assert "35.0 x 72.4 x 4.0 mm" in text
    assert "cylinder diameters 3, 12, 30" in text
    assert "if it already meets them, finish" in text


def test_no_summary_without_a_valid_solid():
    assert cad_workflow.buildable_summary(_state(solids=0)) == ""
    assert cad_workflow.buildable_summary(_state(valid=False)) == ""
    assert cad_workflow.buildable_summary({"objects": {}}) == ""
