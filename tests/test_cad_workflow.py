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


def test_assumption_ledger_rejects_bad_order_and_duplicate_ids():
    rows = (
        _assumption(consequence="low"),
        _assumption(consequence="high"),
    )
    issues = cad_workflow.assumption_ledger_missing(
        SimpleNamespace(assumptions=rows),
        SimpleNamespace(assumptions_accepted=False))
    assert any("duplicate" in issue for issue in issues)
    assert any("sorted" in issue for issue in issues)


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
