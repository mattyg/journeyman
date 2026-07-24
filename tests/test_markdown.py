from freecad.llm_copilot.markdown import to_html


def test_common_model_markdown_is_rendered():
    rendered = to_html(
        "# Result\n\n**Done** with `Part.Box`.\n\n"
        "- First\n- Second\n\n```python\nprint('<safe>')\n```"
    )
    assert "<h1>Result</h1>" in rendered
    assert "<b>Done</b>" in rendered
    assert "<code>Part.Box</code>" in rendered
    assert "<ul><li>First</li><li>Second</li></ul>" in rendered
    assert "&lt;safe&gt;" in rendered


def test_html_and_unsafe_links_are_escaped():
    rendered = to_html("<script>alert(1)</script> [x](javascript:alert(1))")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert 'href="javascript:' not in rendered


def test_first_paragraph_can_render_inline_after_a_chat_label():
    rendered = to_html("First sentence.\n\nSecond paragraph.", inline_first=True)
    assert rendered.startswith("First sentence.")
    assert not rendered.startswith("<p>")
    assert "<p>Second paragraph.</p>" in rendered


def test_long_code_and_prose_tokens_gain_wrap_opportunities():
    token = "VeryLongIdentifier" * 8
    rendered = to_html(f"`{token}`\n\n{token}\n\n```\n{token}\n```")
    assert rendered.count("&#8203;") >= 3
    assert token not in rendered
