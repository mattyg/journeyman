from freecad.llm_copilot.context_usage import estimate_tokens, format_usage


def test_usage_increases_with_conversation():
    base = estimate_tokens([], "system", [{"name": "tool"}])
    used = estimate_tokens(
        [{"role": "user", "content": "make a cube " * 100}],
        "system", [{"name": "tool"}])
    assert used > base


def test_usage_label_reports_estimate_and_message_count():
    label = format_usage(
        [{"role": "user", "content": "hello"}], "system", [])
    assert label.startswith("Context: ~")
    assert "tokens" in label
    assert "1 message" in label
