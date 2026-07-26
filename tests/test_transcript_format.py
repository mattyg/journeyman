from freecad.journeyman.markdown import wrappable_escape, wrapped_pre


def test_wrappable_escape_escapes_html():
    assert wrappable_escape("<b> & 'x'") == "&lt;b&gt; &amp; &#x27;x&#x27;"


def test_wrappable_escape_preserves_whitespace_runs():
    assert wrappable_escape("a  b") == "a  b"


def test_wrappable_escape_breaks_long_tokens_with_zero_width_space():
    token = "a" * 100
    out = wrappable_escape(token, chunk=48)
    assert "&#8203;" in out
    # Two breaks for 100 chars at chunk 48 -> segments of 48/48/4.
    assert out.count("&#8203;") == 2
    assert out.replace("&#8203;", "") == token


def test_wrappable_escape_does_not_break_short_tokens():
    assert "&#8203;" not in wrappable_escape("short", chunk=48)


def test_wrappable_escape_handles_none_and_empty():
    assert wrappable_escape(None) == ""
    assert wrappable_escape("") == ""


def test_wrapped_pre_preserves_leading_indentation_as_nbsp():
    out = wrapped_pre("    indented")
    assert "&nbsp;&nbsp;&nbsp;&nbsp;indented" in out
    assert out.startswith("<div")


def test_wrapped_pre_joins_lines_with_break_tags():
    out = wrapped_pre("one\ntwo")
    assert "one<br>two" in out


def test_wrapped_pre_escapes_content():
    assert "&lt;script&gt;" in wrapped_pre("<script>")
