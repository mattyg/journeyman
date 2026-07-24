from freecad.llm_copilot import turn_protocol
from freecad.llm_copilot.settings import Settings
from freecad.llm_copilot.types import ExecResult


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
