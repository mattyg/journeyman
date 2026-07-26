from freecad.journeyman.history_store import encode, decode
from freecad.journeyman.types import ExecResult


def test_compressed_history_roundtrip_restores_execution_results_and_images():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "views"},
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64,YWJj"}},
    ]}]
    entries = [
        {"kind": "text", "html": "<b>You:</b><p>hello</p>", "widget": object()},
        {"kind": "step", "id": 1, "intent": "Make box", "script": "pass",
         "result": ExecResult(True, "ok", "", True, "valid", False)},
        {"kind": "status", "text": "Thinking"},
    ]
    restored_messages, restored_entries = decode(encode(messages, entries))
    assert restored_messages == messages
    assert len(restored_entries) == 2
    assert restored_entries[1]["result"] == entries[1]["result"]
    assert "widget" not in restored_entries[0]


def test_inspection_marker_keys_survive_the_roundtrip():
    # _model_history reduces superseded inspections using these keys, so a
    # reloaded conversation must compact exactly like a live one.
    messages = [{
        "role": "user", "content": "[inspection result]\nFULL",
        "ephemeral": "inspection", "inspection_query": "Pad",
    }]
    restored_messages, _entries = decode(encode(messages, []))
    assert restored_messages == messages


def test_question_history_persists_answer_but_not_live_callback():
    entry = {
        "kind": "question", "question": "Choose",
        "options": [
            {"id": "a", "label": "A", "description": "First"},
            {"id": "b", "label": "B", "description": "Second"},
        ],
        "recommended_option": "a", "allow_multiple": False,
        "answer": ["a"], "_answer_callback": object(), "widget": object(),
    }
    _messages, restored = decode(encode([], [entry]))
    assert restored[0]["answer"] == ["a"]
    assert "_answer_callback" not in restored[0]
    assert "widget" not in restored[0]


def test_execution_diagnostics_roundtrip():
    result = ExecResult(
        True, "out", "", stderr="stderr",
        console_warnings="warning", console_errors="error")
    _messages, restored = decode(encode([], [{
        "kind": "step", "id": 1, "intent": "Test",
        "script": "pass", "result": result,
    }]))
    assert restored[0]["result"] == result


def test_timeout_decision_persists_without_live_callback():
    entry = {
        "kind": "timeout", "message": "Timed out after 300s",
        "decision": True, "_timeout_callback": object(),
    }
    _messages, restored = decode(encode([], [entry]))
    assert restored == [{
        "kind": "timeout", "message": "Timed out after 300s",
        "decision": True,
    }]
