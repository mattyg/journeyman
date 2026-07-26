from freecad.journeyman import turn_protocol
from freecad.journeyman.settings import Settings
from freecad.journeyman.types import ExecResult


def _settings(**kw):
    base = dict(model="m", api_key="", api_base="", confirm_before_running=True,
                auto_approve_loop=False, max_auto_approved_steps=5,
                self_correction_attempts=3, mandatory_verification=False)
    base.update(kw)
    return Settings(**base)


def test_request_is_durable_without_snapshot():
    assert turn_protocol.request("make a box") == "[request]\nmake a box"


def test_current_context_combines_one_snapshot_and_current_ledger():
    settings = _settings(design_ledger_context=True)
    text = turn_protocol.current_context(
        "SNAP", {"strategy": "part", "stage": "sketch"}, settings)
    assert text.count("SNAP") == 1
    assert text.count("[design ledger]") == 1


def test_inspection_result_stage_tags():
    assert turn_protocol.inspection_result("R") == "[inspection result]\nR"
    assert turn_protocol.inspection_result("R", verify_stage=True) == (
        "[verify-stage inspection result]\nR")


def test_api_reference_blocks():
    assert turn_protocol.api_reference("DOC") == (
        "[installed-version API reference]\nDOC")
    assert turn_protocol.automatic_api_reference("DOC") == (
        "[automatic installed-version API lookup]\nDOC"
        "\nUse this reference to correct the next script.")


def test_failure_feedback_includes_all_diagnostics():
    result = ExecResult(
        ok=False, output="out", error="boom", validation="bad",
        stderr="err", console_warnings="warn", console_errors="cerr")
    text = turn_protocol.failure_feedback(result)
    assert text.startswith("[script failed]\n[script output]\nout\n")
    assert "[standard error]\nerr\n" in text
    assert "[FreeCAD console warnings]\nwarn\n" in text
    assert "[FreeCAD console errors]\ncerr\n" in text
    assert "boom\n" in text
    assert "[validation failed]\nbad\n" in text
    assert text.endswith("do not reply in plain text.")


def test_execution_body_unchanged_document():
    settings = _settings()
    result = ExecResult(ok=True, output="", error="")
    body = turn_protocol.execution_body(
        result, {"objects": {}}, {"objects": {}}, [], settings)
    assert body == "[executed OK]\n[document unchanged]\n"


def test_execution_body_with_change_emits_diff_without_snapshot():
    settings = _settings(enhanced_validation=False)
    result = ExecResult(ok=True, output="", error="")
    before = {"objects": {}}
    after = {"objects": {"Box": {"type": "Part::Box"}}}
    body = turn_protocol.execution_body(
        result, before, after, ["Box"], settings)
    assert "[document diff]\n" in body
    assert "Created: Box" in body
    assert "[new snapshot]" not in body


def test_workflow_tail_renders_warnings_without_persisting_ledger():
    settings = _settings(design_ledger_context=True)
    ledger = {"strategy": "part", "stage": "sketch"}
    tail = turn_protocol.workflow_tail(["fix this"], ledger, settings)
    assert "[CAD workflow review]\n- fix this" in tail
    assert "[design ledger]" not in tail


def test_workflow_tail_empty_when_disabled_and_no_warnings():
    settings = _settings(design_ledger_context=False)
    assert turn_protocol.workflow_tail([], {}, settings) == ""


def test_gate_blocks_carry_tag_issues_and_instruction():
    text = turn_protocol.fidelity_required(["hole is missing", "tab omitted"])
    assert text.startswith("[replica fidelity required]\n")
    assert "- hole is missing\n- tab omitted" in text
    assert "The script was not executed." in text


def test_gate_block_without_issues_omits_blank_bullet_section():
    text = turn_protocol.part_design_required()
    assert text.startswith("[Part Design required]\n")
    assert "\n- " not in text


def test_assumption_clarification_names_blocking_ids():
    text = turn_protocol.assumption_clarification_required(["a1", "a2"])
    assert "Blocking assumption ids: a1, a2." in text
    generic = turn_protocol.assumption_clarification_required()
    assert "Blocking assumption ids" not in generic
    assert "Call ask_user before marking" in generic


def test_every_gate_block_is_tagged_for_logs_and_ui():
    blocks = (
        turn_protocol.part_design_required(),
        turn_protocol.structured_plan_required(["x"]),
        turn_protocol.assumption_ledger_required(["x"]),
        turn_protocol.assumption_clarification_required(["a1"]),
        turn_protocol.assumption_clarification_limit(),
        turn_protocol.invalid_assumption_update(["x"]),
        turn_protocol.fidelity_required(["x"]),
        turn_protocol.part_design_violation(["x"]),
    )
    for block in blocks:
        assert block.startswith("[") and "]\n" in block
        # No HTML or Markdown decoration: the same text serves model and user.
        assert "<" not in block and "**" not in block


def _ledger_row(**kw):
    row = {
        "id": "plate_height", "name": "Plate height", "value": 68.0,
        "unit": "mm", "source": "dimensions2.jpg", "confidence": "medium",
        "consequence": "high", "status": "unverified", "evidence": "",
        "if_wrong": "overall scale wrong",
    }
    row.update(kw)
    return row


def test_ledger_rejection_echoes_what_was_received():
    """A complaint about the ledger must show the ledger, so it self-locates."""
    rows = (_ledger_row(), _ledger_row(id="bolt_d", value=12.0))
    text = turn_protocol.assumption_ledger_required(
        ["assumption 2 needs source"], rows)
    assert text.startswith("[assumption ledger required]\n")
    assert "- assumption 2 needs source" in text
    assert "Ledger received:" in text
    assert "1. plate_height = 68.0 mm" in text
    assert "2. bolt_d = 12.0 mm" in text
    assert "consequence=high" in text


def test_ledger_rejection_without_rows_omits_the_echo():
    text = turn_protocol.assumption_ledger_required(["provide a ledger"])
    assert "Ledger received:" not in text
    assert "Resubmit the script" in text


def test_ledger_echo_survives_a_malformed_row():
    text = turn_protocol.assumption_ledger_required(["bad"], ("not-a-dict",))
    assert "'not-a-dict'" in text


def test_invalid_update_also_echoes_the_ledger():
    text = turn_protocol.invalid_assumption_update(
        ["assumption x was removed"], (_ledger_row(),))
    assert "Ledger received:" in text
    assert "plate_height" in text


def test_blocked_carries_every_objection_in_one_message():
    text = turn_protocol.blocked(
        [turn_protocol.part_design_required(),
         turn_protocol.placeholder_code(["the for block at line 2 does nothing"])],
        ["provide an ordered feature-level plan", "this step builds 2 features"])
    assert "[Part Design required]" in text
    assert "[placeholder code]" in text
    assert "also advisory" in text
    assert "- provide an ordered feature-level plan" in text
    assert "- this step builds 2 features" in text


def test_blocked_without_advisories_omits_the_section():
    text = turn_protocol.blocked([turn_protocol.part_design_required()])
    assert "also advisory" not in text


def test_advisories_block_says_the_step_ran():
    text = turn_protocol.advisories(["this step builds 2 features"])
    assert text.startswith("[workflow advisories]\n")
    assert "- this step builds 2 features" in text
    assert "The step ran." in text
    assert "<" not in text and "**" not in text


def test_no_advisories_renders_nothing():
    assert turn_protocol.advisories([]) == ""
