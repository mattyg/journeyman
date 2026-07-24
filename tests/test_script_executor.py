# tests/test_script_executor.py
from freecad.llm_copilot.script_executor import _annotate_traceback

SCRIPT = "\n".join(f"line{n} = {n}" for n in range(1, 21))


def test_annotates_script_frame_with_marked_source_line():
    text = (
        "Traceback (most recent call last):\n"
        '  File "/mod/script_executor.py", line 84, in run\n'
        "    exec(compile(script, ...))\n"
        '  File "<llm_script>", line 13, in <module>\n'
        "Part.OCCError: NULL shape\n")
    out = _annotate_traceback(SCRIPT, text)
    assert ">>> 13 | line13 = 13" in out
    assert "    11 | line11 = 11" in out
    assert "    15 | line15 = 15" in out
    # Context only: nothing outside the +/-2 window.
    assert "line10" not in out and "line16" not in out
    # The host frame is untouched — only <llm_script> frames get source.
    assert "script_executor.py" in out
    assert out.count(">>>") == 1


def test_annotation_preserves_original_traceback_text():
    text = '  File "<llm_script>", line 2, in <module>\nValueError: boom\n'
    out = _annotate_traceback(SCRIPT, text)
    for original in text.strip().splitlines():
        assert original in out
    assert out.endswith("\n")


def test_clamps_context_at_script_boundaries():
    out = _annotate_traceback(SCRIPT, '  File "<llm_script>", line 1\n')
    assert ">>> 1 | line1 = 1" in out
    assert "line4" not in out  # clamped at the start, no negative lines
    out = _annotate_traceback(SCRIPT, '  File "<llm_script>", line 20\n')
    assert ">>> 20 | line20 = 20" in out


def test_out_of_range_line_is_left_alone():
    text = '  File "<llm_script>", line 99, in <module>\n'
    assert _annotate_traceback(SCRIPT, text) == text


def test_multiple_script_frames_each_annotated():
    text = (
        '  File "<llm_script>", line 3, in <module>\n'
        '  File "<llm_script>", line 7, in helper\n')
    out = _annotate_traceback(SCRIPT, text)
    assert ">>> 3 | line3 = 3" in out
    assert ">>> 7 | line7 = 7" in out
    assert out.count(">>>") == 2


def test_traceback_without_script_frames_is_unchanged():
    text = "Traceback:\n  File \"/other.py\", line 3, in f\nKeyError: 'x'\n"
    assert _annotate_traceback(SCRIPT, text) == text
